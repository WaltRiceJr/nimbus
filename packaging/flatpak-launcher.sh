#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
export PYTHONPATH="/app/lib/nimbus-weather${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m nimbus "$@"
