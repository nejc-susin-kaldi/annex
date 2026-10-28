<?php
// Run by entrypoint.sh on every start, after the migrations; idempotent.
// 1. Disables Kanboard's built-in "admin" account while it still has its default password ("admin"), which the
//    schema creates on first install. People sign in through Annex only (reverse-proxy authentication); a known
//    password would otherwise work on Kanboard's API.
// 2. Makes sure Kanboard has an API token of its own that nobody outside this database knows (kbdb.php). The
//    container's jobs read it from there; the API only answers on 127.0.0.1 anyway.
require __DIR__ . '/kbdb.php';

try {
    $pdo = kanboard_db();
    $row = $pdo->query("SELECT id, password FROM users WHERE username = 'admin'")->fetch(PDO::FETCH_ASSOC);
    if ($row && $row['password'] && password_verify('admin', $row['password'])) {
        $stmt = $pdo->prepare('UPDATE users SET is_active = false, password = ? WHERE id = ?');
        $stmt->execute([password_hash(bin2hex(random_bytes(24)), PASSWORD_BCRYPT), $row['id']]);
        log_line('info', 'default admin account disabled', ['user_id' => (int) $row['id']]);
    }
    [, $regenerated] = kanboard_api_token($pdo);
    if ($regenerated) {
        log_line('info', 'Kanboard API token generated');
    }
} catch (Throwable $e) {
    log_line('error', 'could not check the default admin account and the API token', ['error' => $e->getMessage()]);
    exit(1);
}
