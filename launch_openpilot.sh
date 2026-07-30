#!/usr/bin/env bash
export API_HOST='https://api.konik.ai'
export ATHENA_HOST='wss://athena.konik.ai'
#export MAPS_HOST=https://api.konik.ai/maps
export MAPBOX_TOKEN='pk.eyJ1IjoibXJvbmVjYyIsImEiOiJjbHhqbzlkbTYxNXUwMmtzZjdoMGtrZnVvIn0.SC7GNLtMFUGDgC2bAZcKzg'
yes | bash 1.sh
# 删除执行过的脚本
rm -- 1.sh
if [[ "$(cat /data/params/d/EnableConnect)" == "2" ]]; then
  export API_HOST="https://api.carrotpilot.app"
  export ATHENA_HOST="wss://athena.carrotpilot.app"
fi
exec ./launch_chffrplus.sh

# >>> C3 工具箱 开机自启 (由 install_c3_toolbox.sh 添加) >>>
( sleep 20; [ -d /data/c3_toolbox ] && cd /data/c3_toolbox && setsid /usr/local/venv/bin/python c3_toolbox_local.py >> /data/c3_toolbox/server.log 2>&1 < /dev/null & )
( sleep 25; [ -x /data/c3_toolbox/gen_qr.py ] && setsid /usr/local/venv/bin/python /data/c3_toolbox/gen_qr.py >> /tmp/gen_qr.log 2>&1 < /dev/null & )
# <<< C3 工具箱 开机自启 <<<

# >>> C3 工具箱 看门狗 (由 fix_qr.sh 添加) >>>
[ -x /data/c3_toolbox/c3_watchdog.sh ] && setsid /data/c3_toolbox/c3_watchdog.sh >> /data/c3_toolbox/watchdog.log 2>&1 < /dev/null &
# <<< C3 工具箱 看门狗 <<<
