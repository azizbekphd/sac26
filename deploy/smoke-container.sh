#!/usr/bin/env bash
set -euo pipefail

image=${1:?Supply the container image to smoke test}
temp_dir=$(mktemp -d)
container=''
cleanup() {
  if [[ -n "$container" ]]; then
    docker rm -f "$container" >/dev/null
  fi
  rm -rf "$temp_dir"
}
trap cleanup EXIT
docker build --platform linux/amd64 -t "$image" .
container=$(docker run -d -p 127.0.0.1:8080:8080 "$image")
if ! curl --fail --silent --show-error --retry 15 --retry-connrefused --retry-delay 1 \
    http://127.0.0.1:8080/ -o "$temp_dir/index.html"; then
  docker logs "$container"
  exit 1
fi
grep -Fq 'Planet X and SPHEREx' "$temp_dir/index.html"
curl --fail --silent --show-error -D "$temp_dir/embed.headers" -o /dev/null http://127.0.0.1:8080/embed
grep -Fqi "frame-ancestors 'self' https:" "$temp_dir/embed.headers"
status=$(curl --silent --show-error -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/playback/missing)
test "$status" = 404
