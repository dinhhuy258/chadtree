#!/usr/bin/env bash

set -eu
set -o pipefail


cd "$(dirname "$0")" || exit 1

mypy --ignore-missing-imports -- *.py rplugin/python3/fm/*.py
