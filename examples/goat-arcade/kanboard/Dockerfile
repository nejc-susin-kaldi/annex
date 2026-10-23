# Kanboard, the official release image, adapted to the Annex app contract (see README.md).
#
# Upgrade: change KANBOARD_VERSION and KANBOARD_DIGEST together (the multi-arch index digest of
# kanboard/kanboard:<version>, from `docker buildx imagetools inspect kanboard/kanboard:<version>`), pick a release
# that is at least two weeks old, read its changelog, and deploy. Kanboard migrates its database on start.
ARG KANBOARD_VERSION=v1.2.54
ARG KANBOARD_DIGEST=sha256:8df6c4339134b6c196da9a262daa42ab8ce395f528a48d4a3dd7038c296f34c1
FROM kanboard/kanboard:${KANBOARD_VERSION}@${KANBOARD_DIGEST} AS upstream

# Copy the upstream filesystem into a fresh image. This keeps Kanboard, PHP and nginx exactly as released but
# drops the upstream metadata Annex can't use: its VOLUME lines (Docker would create an anonymous volume per
# container, outside /data and outside the backups), its root entrypoint (s6, which writes into /etc) and its
# health check on port 80.
FROM scratch
COPY --from=upstream / /

ENV HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Annex's settings for Kanboard, nginx and php-fpm (all temporary files go to /tmp, all data to /data).
# The upstream config.php is replaced: ours sets the same two values and the rest of the contract.
COPY annex/config.php /var/www/app/config.php
COPY annex/nginx.conf.tmpl annex/php-fpm.conf annex/entrypoint.sh annex/lockdown.php annex/seed.php annex/cronjob.sh /opt/annex/
RUN chmod 0755 /opt/annex/entrypoint.sh /opt/annex/cronjob.sh \
 && rm -rf /var/www/app/data /etc/nginx/ssl \
 && adduser -D -H -u 10001 -s /sbin/nologin app

# Annex runs every app as uid 10001 on a read-only filesystem
USER 10001
WORKDIR /var/www/app

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s \
  CMD curl -fsS -o /dev/null "http://127.0.0.1:${PORT:-8080}/healthcheck.php" || exit 1

ENTRYPOINT ["/opt/annex/entrypoint.sh"]
