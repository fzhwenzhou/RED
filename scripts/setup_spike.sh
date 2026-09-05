#!/usr/bin/env bash
# =============================================================================
# setup_spike.sh — install the Spike toolchain RED's Gate 2 needs.
#
#   bash scripts/setup_spike.sh          # install (idempotent)
#   bash scripts/setup_spike.sh check    # report what is installed, and where
#
# Gate 2 compiles each ISASpec into a Spike *extension* (a .so loaded with
# --extlib), cross-compiles a bare-metal rv32 program that executes the custom
# instructions, and diffs the ISS results against the designer's C models. That
# needs:
#
#   spike                     the simulator             (built here from source)
#   spike headers             to compile the extension  (kept under share/)
#   riscv64-unknown-elf-gcc   rv32 cross compiler       (distro package)
#   dtc                       spike shells out to it    (distro package)
#
# INSTALL LOCATION — RED_SPIKE_PREFIX, default $HOME/.local:
#   $PREFIX/bin                      spike, spike-dasm, dtc, ...
#   $PREFIX/include                  spike's installed headers
#   $PREFIX/share/riscv-isa-sim/src    source tree   (decode_macros.h et al.)
#   $PREFIX/share/riscv-isa-sim/build  config.h + insn_list.h (generated)
#
# $HOME/.local/bin is on PATH on most distros already (Debian/Ubuntu .profile
# adds it), so `spike` becomes an ordinary command rather than something only
# RED can find. For a system-wide install run with a writable prefix, e.g.
#
#   sudo RED_SPIKE_PREFIX=/usr/local bash scripts/setup_spike.sh
#
# The build itself needs no root: boost is not required (it only guards spike's
# optional socket interface), and when apt is not usable the distro packages are
# unpacked into the prefix instead of installed.
# =============================================================================

set -euo pipefail

PREFIX="${RED_SPIKE_PREFIX:-$HOME/.local}"
SHARE="$PREFIX/share/riscv-isa-sim"
SPIKE_SRC="$SHARE/src"
SPIKE_BUILD="$SHARE/build"
WORK="${RED_SPIKE_WORK:-${TMPDIR:-/tmp}/red-spike-build}"
SPIKE_REPO="${RED_SPIKE_REPO:-https://github.com/riscv-software-src/riscv-isa-sim.git}"
JOBS="${RED_SPIKE_JOBS:-$(( $(nproc) > 4 ? 4 : $(nproc) ))}"

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi
say()  { echo "${B}==>${N} $*"; }
ok()   { echo "  ${G}ok${N} $*"; }
warn() { echo "  ${Y}warning:${N} $*" >&2; }
die()  { echo "  ${R}error:${N} $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

export PATH="$PREFIX/bin:$PATH"

can_sudo() { sudo -n true 2>/dev/null; }

# ---------------------------------------------------------------------------
# distro packages (dtc, riscv gcc), with a no-root fallback
# ---------------------------------------------------------------------------

# `apt-get download` needs no privileges and `dpkg-deb -x` only extracts, so
# this installs the distro's own binaries into the prefix rather than building
# a parallel copy of them by hand.
unpack_deb_binary() {
    local pkg="$1" rel="$2" dest="$3"
    local dl; dl="$(mktemp -d)"
    ( cd "$dl" && apt-get download "$pkg" >/dev/null 2>&1 ) || { rm -rf "$dl"; return 1; }
    ( cd "$dl" && for d in *.deb; do dpkg-deb -x "$d" root; done )
    [ -f "$dl/root/$rel" ] || { rm -rf "$dl"; return 1; }
    install -D -m755 "$dl/root/$rel" "$dest"
    rm -rf "$dl"
}

step_packages() {
    say "toolchain packages (dtc, riscv gcc)"
    if have dtc && have riscv64-unknown-elf-gcc; then
        ok "dtc + riscv64-unknown-elf-gcc already on PATH"; return
    fi
    if can_sudo; then
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            device-tree-compiler gcc-riscv64-unknown-elf
        ok "installed via apt"
    else
        warn "no passwordless sudo — unpacking distro packages into $PREFIX"
        have dtc || unpack_deb_binary device-tree-compiler usr/bin/dtc "$PREFIX/bin/dtc" \
            || die "could not obtain dtc; install device-tree-compiler and re-run"
        # dtc links libfdt statically and needs only libyaml, which is a system
        # library nearly everywhere. Say so plainly if it is not.
        if have dtc && ldd "$(command -v dtc)" 2>/dev/null | grep -q "not found"; then
            die "dtc is missing a shared library: $(ldd "$(command -v dtc)" | grep 'not found')"
        fi
        have riscv64-unknown-elf-gcc \
            || die "gcc-riscv64-unknown-elf is not installed and is too large to unpack safely;
       install it with: sudo apt-get install gcc-riscv64-unknown-elf"
    fi
    have dtc || die "dtc still not on PATH"
    have riscv64-unknown-elf-gcc || die "riscv64-unknown-elf-gcc still not on PATH"
    ok "dtc $(dtc --version 2>&1 | head -1 | tr -d '\n'), $(riscv64-unknown-elf-gcc -dumpversion) cross gcc"
}

# ---------------------------------------------------------------------------
# spike
# ---------------------------------------------------------------------------

step_build() {
    say "spike"
    if [ -x "$PREFIX/bin/spike" ] && [ -f "$SPIKE_BUILD/config.h" ] \
       && [ -f "$SPIKE_SRC/riscv/decode_macros.h" ]; then
        ok "already installed: $($PREFIX/bin/spike --help 2>&1 | head -1)"
        return
    fi
    say "building spike from source (10-25 min the first time)"
    mkdir -p "$WORK"
    if [ -d "$WORK/src/.git" ]; then
        ok "reusing source at $WORK/src"
    else
        rm -rf "$WORK/src"
        git clone --depth 1 "$SPIKE_REPO" "$WORK/src"
    fi
    mkdir -p "$WORK/build"
    cd "$WORK/build"
    # boost is only used by spike's optional socket interface, behind
    # #ifdef HAVE_BOOST_ASIO — configure disables it when boost is absent, so
    # RED never needs boost. dtc, by contrast, is a hard requirement.
    [ -f config.h ] || "$WORK/src/configure" --prefix="$PREFIX" >configure.log 2>&1 \
        || { tail -20 configure.log; die "configure failed (see $WORK/build/configure.log)"; }
    make -j"$JOBS" >build.log 2>&1 || { tail -30 build.log; die "build failed (see $WORK/build/build.log)"; }
    make install >>build.log 2>&1 || die "make install failed"
    ok "installed $PREFIX/bin/spike"

    # Gate 2 compiles its extension against spike's *source* headers: `make
    # install` ships some headers but not decode_macros.h, and config.h /
    # insn_list.h are generated into the build directory. Keep just those —
    # the objects and libraries are ~3.7 GB and nothing needs them afterwards.
    say "keeping the headers Gate 2 compiles against"
    rm -rf "$SHARE"; mkdir -p "$SPIKE_SRC" "$SPIKE_BUILD"
    ( cd "$WORK/src" && tar --exclude=.git -cf - . ) | ( cd "$SPIKE_SRC" && tar -xf - )
    cp "$WORK/build/config.h" "$WORK/build/insn_list.h" "$SPIKE_BUILD/"
    cat > "$SPIKE_BUILD/README" <<'EOF'
Generated spike headers kept so RED's Gate 2 can compile its extension against
them; they are not part of `make install`. Objects and libraries were pruned.
EOF
    ok "headers at $SHARE ($(du -sh "$SHARE" | cut -f1))"
}

step_path() {
    say "PATH"
    case ":$PATH:" in
        *":$PREFIX/bin:"*) ok "$PREFIX/bin is on PATH" ;;
        *)  warn "$PREFIX/bin is NOT on your PATH. Add it:"
            echo "      bash:  echo 'export PATH=\"$PREFIX/bin:\$PATH\"' >> ~/.bashrc"
            echo "      fish:  fish_add_path $PREFIX/bin" ;;
    esac
}

step_verify() {
    say "verifying Gate 2 end to end"
    cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    local py="${RED_VENV:-$PWD/.venv}/bin/python"
    [ -x "$py" ] || py=python3
    "$py" -m red.iss_selftest || die "Gate 2 self-test failed"
}

case "${1:-install}" in
    install)
        step_packages; step_build; step_path; step_verify
        echo; ok "done — spike is on PATH and Gate 2 is ready"
        ;;
    check)
        say "prefix $PREFIX"
        for t in spike riscv64-unknown-elf-gcc dtc; do
            printf "  %-26s %s\n" "$t" "$(command -v $t || echo MISSING)"
        done
        [ -f "$SPIKE_BUILD/config.h" ] && ok "extension headers at $SHARE" \
                                       || warn "no extension headers at $SHARE"
        step_path
        ;;
    *) sed -n '2,32p' "$0"; exit 1 ;;
esac
