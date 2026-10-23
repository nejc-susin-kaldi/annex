<?php
// Kanboard's settings on Annex (replaces the upstream image's config.php, which set only the first two).
// Kanboard reads this before its own defaults (app/constants.php), so these values win over the environment.
// The database comes from DATABASE_URL, which Kanboard parses itself (app/common.php: driver "postgres", host,
// port, user, password, database), so no DB_* settings are needed here.

define('ENABLE_URL_REWRITE', true);
define('LOG_DRIVER', 'stderr');          // php-fpm passes the lines through to the container log

// Storage: everything Kanboard writes goes under /data (storage.files), which Annex backs up
define('DATA_DIR', '/data');
define('FILES_DIR', '/data/files');      // attachments
define('CACHE_DIR', '/data/cache');
define('PLUGINS_DIR', '/data/plugins');
define('PLUGIN_INSTALLER', false);       // no installing from the internet (egress: [])
define('DB_RUN_MIGRATIONS', false);      // entrypoint.sh migrates once at start

// Sign-in through Annex's front door: Remote-User (PHP: HTTP_REMOTE_USER) names the person, Remote-Email and
// Remote-Name fill in the profile. Users are created on their first visit. Note: Kanboard's default header,
// REMOTE_USER, is something else (the HTTP basic-auth user, which a client can choose), so it must not be used.
define('REVERSE_PROXY_AUTH', true);
define('REVERSE_PROXY_USER_HEADER', 'HTTP_REMOTE_USER');
define('REVERSE_PROXY_EMAIL_HEADER', 'HTTP_REMOTE_EMAIL');
define('REVERSE_PROXY_FULLNAME_HEADER', 'HTTP_REMOTE_NAME');
define('REVERSE_PROXY_DEFAULT_ADMIN', getenv('KANBOARD_DEFAULT_ADMIN') ?: 'kaldi-admin');
// Kanboard trusts the header only from these networks: the container's internal networks, which entrypoint.sh
// writes to /tmp at start (a file rather than a variable, so jobs run with `docker exec` see it too)
define('TRUSTED_PROXY_NETWORKS', trim((string) @file_get_contents('/tmp/annex-trusted-networks')));

// Kanboard's own sign-in is off: no login form, no remember-me cookie, no logout (it would sign straight back in)
define('HIDE_LOGIN_FORM', true);
define('REMEMBER_ME_AUTH', false);
define('DISABLE_LOGOUT', true);

// Email only through Annex: Kanboard has no mail server to talk to, so its mail settings are hidden
define('MAIL_CONFIGURATION', false);

// The JSON-RPC token for this container's own jobs (seed.php) is an Annex secret. Without it Kanboard falls
// back to the random token in its settings table, which nobody outside Kanboard's admin page knows.
$annexApiToken = (string) getenv('KANBOARD_API_TOKEN');
if (strlen($annexApiToken) >= 32) {
    define('API_AUTHENTICATION_TOKEN', $annexApiToken);
}
unset($annexApiToken);
