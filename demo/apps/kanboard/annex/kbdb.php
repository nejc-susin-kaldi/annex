<?php
// Shared by lockdown.php and seed.php: Kanboard's own database (DATABASE_URL) and its JSON-RPC API token.

function log_line(string $level, string $msg, array $fields = []): void
{
    echo json_encode(['ts' => gmdate('Y-m-d\TH:i:s\Z'), 'level' => $level, 'msg' => $msg] + $fields,
                     JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE), "\n";
}

function kanboard_db(): PDO
{
    $u = parse_url((string) getenv('DATABASE_URL'));
    $dsn = sprintf('pgsql:host=%s;port=%d;dbname=%s', $u['host'] ?? '', $u['port'] ?? 5432, ltrim($u['path'] ?? '', '/'));
    return new PDO($dsn, urldecode($u['user'] ?? ''), urldecode($u['pass'] ?? ''),
                   [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
}

/**
 * Kanboard's API token, from its settings table (the one Settings > API shows to Kanboard admins). If there is none,
 * or it is the fixed token in Kanboard's published schema dump (app/Schema/Sql/postgres.sql), a new one is made the
 * way Kanboard makes it (Token::getToken(): 30 random bytes as hex) and stored. Returns [token, regenerated].
 */
function kanboard_api_token(PDO $pdo): array
{
    $token = $pdo->query("SELECT value FROM settings WHERE option = 'api_token'")->fetchColumn();
    $dump = @file_get_contents('/var/www/app/app/Schema/Sql/postgres.sql') ?: '';
    $published = preg_match("/\\('api_token', '([0-9a-f]+)'/", $dump, $m) ? $m[1] : null;
    if (is_string($token) && strlen($token) >= 32 && $token !== $published) {
        return [$token, false];
    }
    $new = bin2hex(random_bytes(30));
    if ($token === false) {
        $stmt = $pdo->prepare("INSERT INTO settings (option, value, changed_by, changed_on) VALUES ('api_token', ?, 0, ?)");
        $stmt->execute([$new, time()]);
    } else {
        $stmt = $pdo->prepare("UPDATE settings SET value = ?, changed_by = 0, changed_on = ? WHERE option = 'api_token'");
        $stmt->execute([$new, time()]);
    }
    return [$new, true];
}
