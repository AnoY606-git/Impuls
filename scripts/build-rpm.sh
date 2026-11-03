#!/usr/bin/env bash
set -euo pipefail

version=1.0
wheel_name=yandex_music-3.2.2-py3-none-any.whl
wheel_sha=f2e603530c74a4affd2bead6f42e7b17c5e83732a2dda752206ef6c0d3dbf515
wheel_url=https://files.pythonhosted.org/packages/78/3d/4735a1171d293e6b4bb46e5f271cd4d62f6b74f5c28c494afa99a720df44/yandex_music-3.2.2-py3-none-any.whl
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

# This explicit manifest prevents local credentials, scratch files and other
# untracked content from entering the source RPM or a public release.
source_files=(
  LICENSE README.md
  impuls_player/__init__.py impuls_player/__main__.py impuls_player/app.py
  impuls_player/controller.py impuls_player/mpris.py impuls_player/player.py
  impuls_player/presets.py impuls_player/queue.py impuls_player/secrets.py
  impuls_player/service.py impuls_player/style.css impuls_player/ui.py impuls_player/wave.py
  tests/test_app.py tests/test_controller.py tests/test_library_contract.py
  tests/test_mpris.py tests/test_player.py tests/test_presets.py tests/test_queue.py
  tests/test_secrets.py tests/test_service.py tests/test_source_archive.py
  tests/test_ui.py tests/test_ui_helpers.py tests/test_wave.py
  data/impuls data/io.github.AnoY606_git.Impuls.desktop
  data/io.github.AnoY606_git.Impuls.metainfo.xml data/io.github.AnoY606_git.Impuls.svg
  docs/images/player.png docs/images/visualizer.png
  packaging/impuls.spec scripts/build-rpm.sh
)
create_source() {
  tar -C "$project_root" --transform "s,^,impuls-$version/," -czf "$1" -- "${source_files[@]}"
}
if [[ ${1:-} == --source-only ]]; then
  [[ $# -eq 2 ]] || { printf 'Usage: %s --source-only OUTPUT\n' "$0" >&2; exit 2; }
  create_source "$2"
  exit 0
fi
[[ $# -eq 0 ]] || { printf 'Unexpected argument: %s\n' "$1" >&2; exit 2; }

build_root=$(mktemp -d "${TMPDIR:-/tmp}/impuls-rpm.XXXXXXXX")
trap 'rm -rf -- "$build_root"' EXIT
mkdir -p "$build_root"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS,tmp}
if [[ -n ${IMPULS_WHEEL:-} ]]; then
  cp -- "$IMPULS_WHEEL" "$build_root/SOURCES/$wheel_name"
else
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 2 \
    "$wheel_url" -o "$build_root/SOURCES/$wheel_name"
fi
printf '%s  %s\n' "$wheel_sha" "$build_root/SOURCES/$wheel_name" | sha256sum --check --status
create_source "$build_root/SOURCES/impuls-$version.tar.gz"
cp "$project_root/packaging/impuls.spec" "$build_root/SPECS/impuls.spec"
"${RPMBUILD:-rpmbuild}" -ba --define "_topdir $build_root" --define "_tmppath $build_root/tmp" "$build_root/SPECS/impuls.spec"
mkdir -p "$project_root/dist"
packages=("$build_root"/RPMS/noarch/impuls-"$version"-*.rpm "$build_root"/SRPMS/impuls-"$version"-*.rpm)
output_files=()
for package in "${packages[@]}"; do
  [[ -f $package ]] || { printf 'Missing package: %s\n' "$package" >&2; exit 1; }
  filename=${package##*/}
  cp -- "$package" "$project_root/dist/$filename"
  output_files+=("./$filename")
done
(cd "$project_root/dist" && sha256sum -- "${output_files[@]}" > SHA256SUMS)
printf 'Packages: %s/dist/\n' "$project_root"
