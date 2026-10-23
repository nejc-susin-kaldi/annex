<?php
// The repair board's content, from Goat Arcade's data through the Annex gateway, written with Kanboard's
// JSON-RPC API (as the app user "jsonrpc", with the KANBOARD_API_TOKEN secret).
//
//   php /opt/annex/seed.php seed     (annex run kanboard seed; by hand, once, and safe to repeat)
//     a project per open venue, "Parts to order" (and "Workshop" for machines in storage), each with the columns
//     Reported, Diagnosing, Waiting for parts, Fixed; a card per open service ticket in its venue's project, in
//     the column for its status; a card in "Parts to order" per ticket waiting for parts; then the members sync.
//     Cards carry the reference ticket-<id> (parts-<id>): a rerun adds only what's missing and never moves or
//     changes a card people have worked on.
//   php /opt/annex/seed.php members  (every 15 minutes)
//     makes everyone who has signed in to Kanboard a member of the Goat Arcade projects (Kanboard creates a user
//     on their first visit, with no projects).
//
// Exit 0 on success, 1 on failure (Annex alerts the owner), 2 when the API token isn't set yet.

const COLUMNS = ['Reported', 'Diagnosing', 'Waiting for parts', 'Fixed'];
const STATUS_COLUMN = ['open' => 'Reported', 'in_progress' => 'Diagnosing', 'waiting_parts' => 'Waiting for parts'];
const PARTS = ['identifier' => 'PARTS', 'name' => 'Parts to order',
               'description' => 'Parts to order for machines waiting on them, one card per service ticket.'];
const WORKSHOP = ['identifier' => 'WORKSHOP', 'name' => 'Workshop (machines in storage)',
                  'description' => 'Repairs on machines that are not on a venue floor.'];

function log_line(string $level, string $msg, array $fields = []): void
{
    echo json_encode(['ts' => gmdate('Y-m-d\TH:i:s\Z'), 'level' => $level, 'msg' => $msg] + $fields,
                     JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE), "\n";
}

function http_json(string $url, array $headers, ?string $body = null, ?string $basicAuth = null)
{
    $ch = curl_init($url);
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 30, CURLOPT_HTTPHEADER => $headers]);
    if ($body !== null) {
        curl_setopt_array($ch, [CURLOPT_POST => true, CURLOPT_POSTFIELDS => $body]);
    }
    if ($basicAuth !== null) {
        curl_setopt($ch, CURLOPT_USERPWD, $basicAuth);
    }
    $raw = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    $err = curl_error($ch);
    if ($raw === false) {
        throw new RuntimeException("request to " . parse_url($url, PHP_URL_PATH) . " failed: $err");
    }
    if ($code !== 200) {
        throw new RuntimeException("request to " . parse_url($url, PHP_URL_PATH) . " returned HTTP $code");
    }
    return json_decode($raw, true, 512, JSON_THROW_ON_ERROR);
}

function gateway(string $view, array $params = []): array
{
    $base = rtrim((string) getenv('ANNEX_GATEWAY_URL'), '/');
    $url = "$base/$view" . ($params ? '?' . http_build_query($params) : '');
    return http_json($url, ['Authorization: Bearer ' . getenv('ANNEX_GATEWAY_TOKEN'), 'Accept: application/json']);
}

final class Kanboard
{
    private int $id = 0;

    public function __construct(private string $url, private string $token) {}

    public function __call(string $method, array $args)
    {
        $params = $args[0] ?? [];
        $body = json_encode(['jsonrpc' => '2.0', 'method' => $method, 'id' => ++$this->id, 'params' => $params]);
        $res = http_json($this->url, ['Content-Type: application/json'], $body, 'jsonrpc:' . $this->token);
        if (isset($res['error'])) {
            throw new RuntimeException("Kanboard API $method: " . ($res['error']['message'] ?? 'error'));
        }
        return $res['result'] ?? null;
    }
}

/** The project with this identifier, created with the board's columns if it doesn't exist yet. */
function ensure_project(Kanboard $kb, array $p, array &$stats): array
{
    $project = $kb->getProjectByIdentifier(['identifier' => $p['identifier']]);
    if (!$project) {
        $id = $kb->createProject(['name' => $p['name'], 'description' => $p['description'],
                                  'identifier' => $p['identifier']]);
        if (!$id) {
            throw new RuntimeException("Kanboard API createProject: project {$p['identifier']} was not created");
        }
        $project = $kb->getProjectById(['project_id' => $id]);
        $stats['projects_created']++;
        // a new project comes with Kanboard's default columns: rename them to ours, drop the rest
        $existing = $kb->getColumns(['project_id' => $id]);
        foreach (COLUMNS as $i => $title) {
            if (isset($existing[$i])) {
                $kb->updateColumn(['column_id' => (int) $existing[$i]['id'], 'title' => $title, 'task_limit' => 0,
                                   'description' => '']);
            } else {
                $kb->addColumn(['project_id' => $id, 'title' => $title]);
            }
        }
        foreach (array_slice($existing, count(COLUMNS)) as $extra) {
            $kb->removeColumn(['column_id' => (int) $extra['id']]);
        }
    } else {
        // an existing project keeps its columns; one of ours that was removed or renamed comes back at the end
        $titles = array_column($kb->getColumns(['project_id' => (int) $project['id']]), 'title');
        foreach (COLUMNS as $title) {
            if (!in_array($title, $titles, true)) {
                $kb->addColumn(['project_id' => (int) $project['id'], 'title' => $title]);
            }
        }
    }
    return $project;
}

function column_ids(Kanboard $kb, int $projectId): array
{
    $ids = [];
    foreach ($kb->getColumns(['project_id' => $projectId]) as $c) {
        $ids[$c['title']] = (int) $c['id'];
    }
    return $ids;
}

function ensure_card(Kanboard $kb, int $projectId, array $columns, string $column, string $reference, string $title,
                     string $description, string $color, array &$stats): void
{
    if ($kb->getTaskByReference(['project_id' => $projectId, 'reference' => $reference])) {
        $stats['cards_existing']++;
        return;
    }
    $id = $kb->createTask(['title' => mb_substr($title, 0, 190), 'project_id' => $projectId,
                           'column_id' => $columns[$column] ?? $columns[COLUMNS[0]], 'reference' => $reference,
                           'description' => $description, 'color_id' => $color]);
    if (!$id) {
        throw new RuntimeException("Kanboard API createTask: card $reference was not created");
    }
    $stats['cards_created']++;
}

function tech_board_url(): string
{
    $base = (string) getenv('ANNEX_BASE_URL');
    $app = (string) getenv('ANNEX_APP_NAME') ?: 'kanboard';
    return getenv('TECH_BOARD_URL') ?: preg_replace('#^(https?://)' . preg_quote($app, '#') . '\.#', '$1tech-board.', $base);
}

function describe(array $t, string $venue, string $techBoard): string
{
    $off = $t['took_machine_off_floor'] ? 'yes, out of order' : 'no, still playable';
    $lines = [
        "**Service ticket #{$t['ticket_id']}**: {$t['title']}, serial {$t['serial_no']}, at $venue.",
        '',
        "- Reported: " . str_replace("T", " ", substr((string) $t["opened_at"], 0, 16)) . " by " . ($t['reported_by'] ?: 'unknown'),
        "- Symptom: {$t['symptom']}",
        "- Off the floor: $off",
        "- Technician: " . ($t['technician'] ?: 'not assigned yet'),
        "- Status in the service log when this card was made: {$t['status']}",
        '',
        "Seeded from the service log. The technicians' morning list is on [tech-board]($techBoard).",
    ];
    return implode("\n", $lines);
}

function sync_members(Kanboard $kb, array $identifiers, array &$stats): void
{
    $users = array_filter($kb->getAllUsers() ?: [], fn ($u) => (int) $u['is_active'] === 1 && $u['username'] !== 'admin');
    foreach ($identifiers as $identifier) {
        $project = $kb->getProjectByIdentifier(['identifier' => $identifier]);
        if (!$project) {
            continue;
        }
        $members = $kb->getProjectUsers(['project_id' => (int) $project['id']]) ?: [];
        foreach ($users as $u) {
            if (!isset($members[$u['id']]) && !isset($members[(string) $u['id']])) {
                $kb->addProjectUser(['project_id' => (int) $project['id'], 'user_id' => (int) $u['id'],
                                     'role' => 'project-member']);
                $stats['memberships_added']++;
            }
        }
    }
}

function goat_identifiers(Kanboard $kb): array
{
    // the projects this job made: venue codes, PARTS and WORKSHOP (Kanboard stores identifiers in upper case)
    $ids = [];
    foreach ($kb->getAllProjects() ?: [] as $p) {
        $identifier = (string) ($p['identifier'] ?? '');
        $ours = in_array($identifier, [PARTS['identifier'], WORKSHOP['identifier']], true)
            || str_contains((string) $p['description'], 'Goat Arcade repair board');
        if ($identifier !== '' && (int) $p['is_active'] === 1 && $ours) {
            $ids[] = $identifier;
        }
    }
    return $ids;
}

$mode = $argv[1] ?? 'seed';
if (!in_array($mode, ['seed', 'members'], true)) {
    fwrite(STDERR, "usage: php seed.php seed|members\n");
    exit(1);
}
$started = microtime(true);
$token = (string) getenv('KANBOARD_API_TOKEN');
if (strlen($token) < 32) {
    $how = 'openssl rand -hex 32 | annex secret set kanboard KANBOARD_API_TOKEN';
    if ($mode === 'members') {
        // not configured yet is not a failure of this scheduled job: say so, don't alert every 15 minutes
        log_line('warn', 'members sync skipped: the KANBOARD_API_TOKEN secret is not set', ['fix' => $how]);
        exit(0);
    }
    log_line('error', 'the KANBOARD_API_TOKEN secret is not set (32+ characters); a human sets it', ['fix' => $how]);
    exit(2);
}
$kb = new Kanboard('http://127.0.0.1:' . (getenv('PORT') ?: '8080') . '/jsonrpc.php', $token);
$stats = ['projects_created' => 0, 'cards_created' => 0, 'cards_existing' => 0, 'memberships_added' => 0];
log_line('info', "$mode started");

try {
    if ($mode === 'members') {
        sync_members($kb, goat_identifiers($kb), $stats);
        log_line('info', 'members finished', ['memberships_added' => $stats['memberships_added'], 'ms' => (int) ((microtime(true) - $started) * 1000)]);
        exit(0);
    }

    $today = date('Y-m-d');
    $venues = [];
    foreach (gateway('venues', ['order' => 'venue_code']) as $v) {
        if ($v['opened_on'] !== null && $v['opened_on'] <= $today) {
            $venues[$v['venue_code']] = $v;
        }
    }
    $tickets = gateway('service_log', ['status' => 'neq.closed', 'order' => 'ticket_id']);
    $techBoard = tech_board_url();

    $projects = [];
    foreach ($venues as $code => $v) {
        $projects[$code] = ensure_project($kb, [
            'identifier' => strtoupper(preg_replace('/[^A-Za-z0-9]/', '', $code)),
            'name' => "{$v['venue_name']}, {$v['city']} ($code)",
            'description' => "Goat Arcade repair board for {$v['venue_name']} ($code): a card per broken machine.",
        ], $stats);
    }
    $parts = ensure_project($kb, PARTS, $stats);
    $workshop = null;

    $skipped = 0;
    foreach ($tickets as $t) {
        $code = $t['current_venue_code'];
        if ($code === null) {
            $workshop ??= ensure_project($kb, WORKSHOP, $stats);
            $project = $workshop;
            $venue = 'the workshop (in storage)';
        } elseif (isset($projects[$code])) {
            $project = $projects[$code];
            $venue = "{$venues[$code]['venue_name']} ($code)";
        } else {
            $skipped++; // a venue that isn't open (yet): no board for it
            continue;
        }
        $column = STATUS_COLUMN[$t['status']] ?? COLUMNS[0];
        $desc = describe($t, $venue, $techBoard);
        $color = $t['took_machine_off_floor'] ? 'red' : 'yellow';
        $pid = (int) $project['id'];
        ensure_card($kb, $pid, column_ids($kb, $pid), $column, "ticket-{$t['ticket_id']}",
                    "{$t['title']}: {$t['symptom']}", $desc, $color, $stats);
        if ($t['status'] === 'waiting_parts') {
            $ppid = (int) $parts['id'];
            ensure_card($kb, $ppid, column_ids($kb, $ppid), 'Waiting for parts', "parts-{$t['ticket_id']}",
                        "Parts for {$t['title']} ($venue): {$t['symptom']}", $desc, 'orange', $stats);
        }
    }

    sync_members($kb, goat_identifiers($kb), $stats);
    log_line('info', 'seed finished', $stats + ['venues' => count($venues), 'open_tickets' => count($tickets),
             'skipped_closed_venue' => $skipped, 'ms' => (int) ((microtime(true) - $started) * 1000)]);
} catch (Throwable $e) {
    log_line('error', "$mode failed", ['error' => $e->getMessage()] + $stats);
    exit(1);
}
