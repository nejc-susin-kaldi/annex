FROM busybox:1.37.0-musl AS busybox
FROM twinproduction/gatus:v5.37.0
# the official image has no shell or wget; a static busybox wget runs the container healthcheck
# (busybox picks the applet from its own name, so /bin/wget behaves as wget)
COPY --from=busybox /bin/busybox /bin/wget
