# Installing Spike for RED's Gate 2

Gate 2 differential-tests every designed instruction against a **patched Spike
ISS**: RED compiles the ISS models into a Spike extension, cross-compiles a
bare-metal rv32 program that executes the custom instructions, and diffs the
simulator's results against the designer's C models over 100,000 vectors.

This page covers installing that toolchain. Everything here is done for you by
one command:

```bash
bash scripts/setup_spike.sh
```

---

## 1. What gets installed, and where

`RED_SPIKE_PREFIX` decides the location; it defaults to **`~/.local`**, which is
already on PATH on Debian/Ubuntu (`~/.profile` adds it when the directory
exists), so `spike` becomes an ordinary command:

| Path | What |
|------|------|
| `~/.local/bin/spike`, `spike-dasm`, `elf2hex`, … | the simulator |
| `~/.local/bin/dtc` | device-tree compiler — Spike shells out to it at startup |
| `~/.local/include/{riscv,fesvr,softfloat,fdt}` | Spike's installed headers |
| `~/.local/share/riscv-isa-sim/src` | Spike's source tree (~11 MB) |
| `~/.local/share/riscv-isa-sim/build` | `config.h` + `insn_list.h` (generated) |

The source tree is kept because Gate 2's extension needs headers that
`make install` does **not** ship (`decode_macros.h`) plus two headers generated
into the build directory. Object files and libraries are pruned — they total
~3.7 GB and nothing needs them once Spike is linked.

`riscv64-unknown-elf-gcc` comes from the distro (`gcc-riscv64-unknown-elf`); RED
uses its `rv32im/ilp32` multilib and builds freestanding, so no newlib is needed.

### System-wide instead

```bash
sudo RED_SPIKE_PREFIX=/usr/local bash scripts/setup_spike.sh
```

RED finds Spike through `RED_SPIKE_PREFIX` (or `RED_SPIKE`, `RED_SPIKE_SRC`,
`RED_SPIKE_BUILD` individually), so any prefix works.

---

## 2. Requirements, and two that are commonly assumed but not needed

**Needed:** a C++17 compiler, `make`, `git`, `device-tree-compiler`, and
`gcc-riscv64-unknown-elf`.

**Not needed:**

- **Boost.** Spike's README asks for `libboost-regex-dev libboost-system-dev`,
  but boost appears only in `riscv/socketif.h`, entirely behind
  `#ifdef HAVE_BOOST_ASIO`. Configure detects its absence and disables the
  optional socket interface; the build then succeeds and Gate 2 never touches
  that feature.
- **Root.** Spike builds and installs into a user prefix. `dtc` is a hard
  requirement of both configure and runtime, but if `sudo` is unavailable the
  script fetches the distro's own package with `apt-get download` and unpacks
  the binary with `dpkg-deb -x` — no compiling of dtc, no root. (Ubuntu's `dtc`
  links libfdt statically and needs only `libyaml`, which is a system library.)

---

## 3. Manual installation

If you would rather not run the script:

```bash
# 1. dependencies
sudo apt-get install -y device-tree-compiler gcc-riscv64-unknown-elf

# 2. build spike
git clone --depth 1 https://github.com/riscv-software-src/riscv-isa-sim.git
mkdir spike-build && cd spike-build
../riscv-isa-sim/configure --prefix=$HOME/.local
make -j4 && make install

# 3. keep the headers Gate 2 compiles against
mkdir -p ~/.local/share/riscv-isa-sim/{src,build}
(cd ../riscv-isa-sim && tar --exclude=.git -cf - .) \
    | (cd ~/.local/share/riscv-isa-sim/src && tar -xf -)
cp config.h insn_list.h ~/.local/share/riscv-isa-sim/build/

# 4. PATH (skip if ~/.local/bin is already there)
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc   # bash
fish_add_path ~/.local/bin                                  # fish
```

Without root, replace step 1's `dtc` with:

```bash
cd $(mktemp -d) && apt-get download device-tree-compiler
dpkg-deb -x device-tree-compiler_*.deb root
install -D -m755 root/usr/bin/dtc ~/.local/bin/dtc
```

---

## 4. Verifying

```bash
bash scripts/setup_spike.sh check     # where everything resolved to
spike --help | head -1                # Spike RISC-V ISA Simulator 1.1.1-dev
dtc --version                         # Version: DTC 1.7.0
python -m red.iss_selftest            # the real check
```

`iss_selftest` is the one that matters. It builds a Spike extension,
cross-compiles a bare-metal rv32 program, executes a custom instruction on the
simulator, and checks Gate 2 in **both** directions — a gate that cannot fail is
worth nothing:

```
[PASS] agreeing models    -> agree on 100,000 random + corner vectors
[PASS] disagreeing models -> DISAGREE on vector #5, output word 2:
         C model : 4d85252d 4d85252e 4d85252f 4d852530
         Spike   : 4d85252d 4d85252e 4d852590 4d852530
         input   : 9943b4ab 1502cb40 c13743d5 00f31913
```

The second case injects a bug that only fires on odd accumulators, so it clears
the corner vectors and is caught in the random stream — which exercises the
two-phase (block checksum, then per-word dump) search that produces the exact
counterexample.

You can also confirm the simulator really executed a custom instruction, by
disassembling a harness a real run left behind:

```bash
riscv64-unknown-elf-objdump -d output/work/*/*/gate2/*/rv32_blocks.elf | grep insn
#   800000bc:  01cf878b  .insn 4, 0x01cf878b
```

`0x01cf878b` is opcode `0x0b` (custom-0), funct3 `000`, funct7 `0000000` — the
encoding straight from the ISASpec. objdump prints `.insn` instead of a mnemonic
because the instruction is not in the base ISA; Spike ran it through the
extension RED generated.

---

## 5. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `configure: error: device-tree-compiler not found` | `dtc` is a hard requirement — install it or let the script unpack it |
| `Could not find a version of the Boost libraries` | Harmless. Configure continues and disables the socket interface; Gate 2 does not use it |
| `spike: command not found` after install | `$PREFIX/bin` is not on PATH — `fish_add_path ~/.local/bin`, or add it in `~/.bashrc` |
| Gate 2 says "needs a patched-Spike toolchain" | Run `bash scripts/setup_spike.sh check`; if the prefix is non-default, export `RED_SPIKE_PREFIX` |
| `decode_macros.h: No such file` when Gate 2 builds its extension | The source tree under `share/riscv-isa-sim/src` is missing; re-run the setup script |
| Build killed / out of memory | Lower parallelism: `RED_SPIKE_JOBS=2 bash scripts/setup_spike.sh` |
| Disk pressure during the build | The build tree peaks near 4 GB in `$TMPDIR`; point it elsewhere with `RED_SPIKE_WORK=/path` |
