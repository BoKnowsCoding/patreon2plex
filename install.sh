#! /bin/bash

# copies the scripts into a folder in /opt/ and sets up virtual environment
# I have a cron job set to run from there, and I only want to run stable versions.

INSTALL_DIR="/opt/patreon2plex"
cd "$(dirname $0)"

# scripts
rm -Rf $INSTALL_DIR
mkdir -p "$INSTALL_DIR"
cp ./patreon2plex.py "$INSTALL_DIR"
