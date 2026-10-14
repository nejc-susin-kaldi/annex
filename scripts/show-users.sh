#!/usr/bin/env bash
# Print the demo users' generated passwords (they live only on the VM).
source "$(dirname "$0")/lib.sh"
vm "cat $DATA/secrets/users.txt"
