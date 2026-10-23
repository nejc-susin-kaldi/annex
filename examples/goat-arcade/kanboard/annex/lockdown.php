<?php
// Disables Kanboard's built-in "admin" account while it still has its default password ("admin"), which the
// schema creates on first install. People sign in through Annex only (reverse-proxy authentication); a known
// password would otherwise work on Kanboard's API. Idempotent: run by entrypoint.sh on every start.

function log_line(string $level, string $msg, array $fields = []): void
{
    echo json_encode(['ts' => gmdate('Y-m-d\TH:i:s\Z'), 'level' => $level, 'msg' => $msg] + $fields), "\n";
}

$u = parse_url((string) getenv('DATABASE_URL'));
$dsn = sprintf('pgsql:host=%s;port=%d;dbname=%s', $u['host'], $u['port'] ?? 5432, ltrim($u['path'] ?? '', '/'));
try {
    $pdo = new PDO($dsn, urldecode($u['user'] ?? ''), urldecode($u['pass'] ?? ''), [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
    $row = $pdo->query("SELECT id, password, is_active FROM users WHERE username = 'admin'")->fetch(PDO::FETCH_ASSOC);
    if ($row && $row['password'] && password_verify('admin', $row['password'])) {
        $stmt = $pdo->prepare('UPDATE users SET is_active = false, password = ? WHERE id = ?');
        $stmt->execute([password_hash(bin2hex(random_bytes(24)), PASSWORD_BCRYPT), $row['id']]);
        log_line('info', 'default admin account disabled', ['user_id' => (int) $row['id']]);
    }
} catch (Throwable $e) {
    log_line('error', 'could not check the default admin account', ['error' => $e->getMessage()]);
    exit(1);
}
