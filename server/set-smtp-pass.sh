#!/usr/bin/env bash
# Imposta PW_SMTP_PASS in powerwatch.env senza che la password compaia a
# schermo, nella history della shell o nella riga di comando (che sarebbe
# visibile in "ps"). Passa dall'ambiente, leggibile solo da questo utente.
set -euo pipefail
cd "$(dirname "$0")"

read -rsp "App password iCloud (non viene mostrata): " PW
echo

if [ -z "$PW" ]; then echo "vuota, non cambio nulla"; exit 1; fi

PW="$PW" python3 - <<'PY'
import os, re
p = "powerwatch.env"
s = open(p).read()
s = re.sub(r"^PW_SMTP_PASS=.*$", "PW_SMTP_PASS=" + os.environ["PW"], s, flags=re.M)
open(p, "w").write(s)
PY
unset PW
chmod 600 powerwatch.env

echo "password salvata, riavvio il container"
docker compose up -d
