#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output_dir="${1:-$repo_root/dist}"
signing_public_key="${WORKSTATION_RELEASE_PUBLIC_KEY_FILE:-}"
if [[ -z "$signing_public_key" || ! -f "$signing_public_key" ]]; then
  echo "Set WORKSTATION_RELEASE_PUBLIC_KEY_FILE to the offline Ed25519 public key." >&2
  exit 2
fi
PYTHONPATH="$repo_root" python3 -c '
import pathlib, sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
key = load_pem_public_key(pathlib.Path(sys.argv[1]).read_bytes())
if not isinstance(key, Ed25519PublicKey):
    raise SystemExit("release signing public key must be Ed25519")
' "$signing_public_key"
version="$(PYTHONPATH="$repo_root" python3 -c 'from workstation.version import WORKSTATION_VERSION; print(WORKSTATION_VERSION)')"
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT

package_root="$stage/lmc-ai-workstation_${version}_amd64"
release_root="$package_root/opt/lmc-ai-workstation/releases/$version"
install -d "$package_root/DEBIAN" "$release_root" \
  "$package_root/lib/systemd/system" "$package_root/usr/share/applications" \
  "$package_root/usr/share/lmc-ai-workstation" "$package_root/etc/apt/apt.conf.d" \
  "$package_root/etc/lmc-ai-workstation" \
  "$package_root/etc/systemd/system/ollama.service.d"

cp -a "$repo_root/workstation" "$release_root/workstation"
find "$release_root/workstation" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$release_root/workstation" -depth -type d -name '__pycache__' -empty -delete
find "$release_root/workstation/tests" -depth -delete
install -m 0644 "$repo_root/ai_model_config.py" "$repo_root/ai_name.py" \
  "$repo_root/system_limits.py" "$release_root/"
install -d "$release_root/core" "$release_root/tools"
install -m 0644 "$repo_root/core/__init__.py" "$repo_root/core/media_probe.py" \
  "$release_root/core/"
install -m 0755 "$repo_root/tools/prepare_gpt_sovits_dataset.py" \
  "$release_root/tools/"
find "$release_root" -type d -exec chmod a-s,a-t,go-w {} +
find "$release_root" -type f -exec chmod a-s,a-t,go-w {} +
cp -a "$repo_root/workstation/packaging/debian/." "$package_root/DEBIAN/"
sed -i "s/^Version:.*/Version: $version/" "$package_root/DEBIAN/control"
install -m 0644 "$repo_root/workstation/systemd/"*.service \
  "$repo_root/workstation/systemd/"*.timer "$package_root/lib/systemd/system/"
install -m 0644 "$repo_root/workstation/packaging/lmc-ai-workstation.desktop" \
  "$package_root/usr/share/applications/"
install -m 0644 "$repo_root/workstation/config/config.example.json" \
  "$package_root/usr/share/lmc-ai-workstation/"
install -m 0644 "$signing_public_key" \
  "$package_root/usr/share/lmc-ai-workstation/release-signing-public-key.pem"
install -m 0640 "$repo_root/workstation/config/config.example.json" \
  "$package_root/etc/lmc-ai-workstation/config.json"
install -m 0644 "$repo_root/workstation/packaging/52lmc-ai-workstation-unattended" \
  "$package_root/etc/apt/apt.conf.d/"
install -m 0644 \
  "$repo_root/workstation/packaging/ollama.service.d/lmc-ai-workstation.conf" \
  "$package_root/etc/systemd/system/ollama.service.d/"
chmod 0755 "$package_root/DEBIAN/postinst" "$package_root/DEBIAN/prerm" "$package_root/DEBIAN/postrm"
touch "$release_root/release.ready"
ln -s "releases/$version" "$package_root/opt/lmc-ai-workstation/current"

(
  cd "$release_root"
  find . -type f ! -name release-files.sha256 -print0 | sort -z | \
    sed -z 's#^\./##' | xargs -0 sha256sum
) > "$release_root/release-files.sha256"
chmod 0644 "$release_root/release-files.sha256"
install -d "$output_dir"
dpkg-deb --root-owner-group --build "$package_root" \
  "$output_dir/lmc-ai-workstation_${version}_amd64.deb"
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner \
  -C "$release_root" -czf "$output_dir/lmc-ai-workstation_${version}.tar.gz" .
echo "$output_dir/lmc-ai-workstation_${version}_amd64.deb"
echo "$output_dir/lmc-ai-workstation_${version}.tar.gz"
