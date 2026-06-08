FROM lscr.io/linuxserver/sabnzbd:latest

# Add mkvmerge (from MKVToolNix) so the post-processing script can run.
# The LinuxServer.io image is Alpine-based, so use apk.
RUN apk add --no-cache mkvtoolnix acl
