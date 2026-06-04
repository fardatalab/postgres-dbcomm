#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${1:-${SCRIPT_DIR}/build}"
APP_NAME="printtup_dpa_device"
DEVICE_ARCHIVE="${BUILD_DIR}/${APP_NAME}.a"
HOST_BINARY="${BUILD_DIR}/printtup_dpa_host"
DPACC="${DPACC:-/opt/mellanox/doca/tools/dpacc}"
DPACC_MCPU="${DPACC_MCPU:-nv-dpa-bf3}"
DEVICECC_OPTIONS="-Wall,-Wextra,-Wpedantic,-O3,-DE_MODE_LE,-ffreestanding,-mabi=lp64,-mno-relax,-mcmodel=medany,-nostdlib,-Wdouble-promotion,-DFLEXIO_DEV_ALLOW_EXPERIMENTAL_API,-DFLEXIO_ALLOW_EXPERIMENTAL_API,-I${SCRIPT_DIR},-I/opt/mellanox/flexio/include,-I/opt/mellanox/doca/include"

if [[ -n "${PRINTTUP_DPA_DEVICECC_OPTIONS:-}" ]]; then
	DEVICECC_OPTIONS="${DEVICECC_OPTIONS},${PRINTTUP_DPA_DEVICECC_OPTIONS}"
fi

mkdir -p "${BUILD_DIR}"

export PKG_CONFIG_PATH="/opt/mellanox/doca/lib/$(arch)-linux-gnu/pkgconfig:/opt/mellanox/flexio/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

echo "[printtup_dpa] Building DPA device archive -> ${DEVICE_ARCHIVE}"
"${DPACC}" "${SCRIPT_DIR}/printtup_dpa_device.c" \
	-o "${DEVICE_ARCHIVE}" \
	--mcpu="${DPACC_MCPU}" \
	-hostcc=gcc \
	-hostcc-options="-Wno-deprecated-declarations" \
	--devicecc-options="${DEVICECC_OPTIONS}" \
	--device-libs="-L/opt/mellanox/flexio/lib/bf3" \
	--app-name="${APP_NAME}"

echo "[printtup_dpa] Building Arm host harness -> ${HOST_BINARY}"
g++ -O3 -std=c++20 \
	-Wall -Wextra -Wno-deprecated-declarations \
	-DFLEXIO_ALLOW_EXPERIMENTAL_API -DDOCA_ALLOW_EXPERIMENTAL_API \
	-I"${SCRIPT_DIR}" -I/opt/mellanox/flexio/include -I/opt/mellanox/doca/include \
	"${SCRIPT_DIR}/printtup_dpa_host.cpp" \
	"${DEVICE_ARCHIVE}" \
	-o "${HOST_BINARY}" \
	$(pkg-config --libs doca-common libflexio) \
	-libverbs -lmlx5 -lpthread

echo "[printtup_dpa] Done."
echo "[printtup_dpa] Example: ${HOST_BINARY} --dump /tmp/printtup_binary_dump_sample_200.bin --threads 1,2,4,8 --iterations 1000 --csv /tmp/printtup_dpa_results.csv"
