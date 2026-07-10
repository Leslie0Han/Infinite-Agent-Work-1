#!/bin/bash

DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "${DIR}/mac-启动服务.sh" 2>/dev/null
exec "${DIR}/mac-启动服务.sh"
