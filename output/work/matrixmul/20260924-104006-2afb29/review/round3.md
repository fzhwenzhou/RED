# Spec review

Reviewers: implementability, callability, benefit, legality
Findings: 8 blocking, 1 major, 0 minor

## [BLOCKING] `fmatmul2_custom` — benefit+implementability
**Finding.** The instruction performs floating-point arithmetic on raw integer bit patterns, producing completely incorrect results for the workload's integer matrices.
**Evidence.** The workload operates on integer matrices (initialized with integer literals like `setElement(1, 1, 2, &A)`). The C model uses `memcpy` to copy these integer bytes into `float` arrays, which bitcasts small integers into floating-point denormals (e.g., 2 becomes ~2.8e-45). Multiplying these yields zero, silently breaking the application's arithmetic.
**Suggested fix.** Change the arithmetic in the C model and pseudocode to use standard 32-bit integer addition and multiplication.

## [BLOCKING] `fmatmul2_custom` — implementability
**Finding.** The instruction requires floating-point functional units that the area-optimized PicoRV32 core does not possess, resulting in a disproportionate area cost.
**Evidence.** PicoRV32 only contains a 32-bit integer multiplier. Implementing an instruction that performs IEEE-754 single-precision floating-point arithmetic would require massive hardware additions (FP multipliers and adders) that dwarf the tiny core, which is entirely unjustified given the workload only requires integer math.
**Suggested fix.** Design the instruction around 32-bit integer operations to allow scaling from the core's existing integer hardware.

## [BLOCKING] `fmatmul2_custom` — callability
**Finding.** The instruction performs floating-point arithmetic on bitcasted memory, which will yield garbage results because the workload operates on integers.
**Evidence.** The application's `main::hot` initializes the matrices with integer values (e.g., `setElement(1, 1, 2, &A)`). The instruction's `c_model` and pseudocode directly read memory into IEEE-754 floats via bitcast (`memcpy(A, &in[0], 16)` or `as_float(in[0])`), execute floating-point math, and bitcast back. Reinterpreting a small integer like 2 as a float yields a denormalized value (2.8E-45), meaning the application cannot use this instruction to accelerate its integer workload without adding expensive integer-to-float conversions before and after execution.
**Suggested fix.** Change the arithmetic in the `c_model` and pseudocode to use standard 32-bit scalar integers (`int32_t`), matching the core's native types and the workload's data types.

## [BLOCKING] `fmatmul2_custom` — callability
**Finding.** Computing an entire 2x2 matrix product in a single hardware invocation requires parallel multiplier area that massively exceeds the constraints of the minimal target core.
**Evidence.** The `c_model` computes 8 independent multiply-add operations (a complete 2x2 matrix multiplication) in one invocation. The PicoRV32 core is heavily area-optimized (its entire register file is ~1024 flops) and relies on a sequential multiplier. Instantiating 8 parallel multipliers (especially floating-point ones) in a coprocessor would increase area by orders of magnitude, making it unbuildable for this microarchitecture.
**Suggested fix.** Redesign the instruction to compute a smaller unit of work per invocation (e.g., a single 2-word dot product calculating one matrix cell per call), allowing the coprocessor to reuse a sequential integer multiplier and fit within the core's area budget.

## [BLOCKING] `fmatmul2_custom` — benefit
**Finding.** The instruction deliberately falsifies its invocation count, claiming 8 invocations for an operation that computes the entire result matrix in a single call.
**Evidence.** The description admits: 'While the instruction processes the whole 2x2 matrix in a single hardware invocation, invocations is declared as 8... to bypass a heuristic'. An instruction that replaces the entire 3-deep loop nest to compute all 4 output elements at once is invoked exactly 1 time per kernel call, not 8.
**Suggested fix.** Set invocations to 1. If replacing the entire kernel is rejected by the performance gate's heuristics, design an instruction that replaces an inner loop instead (e.g., a dot product calculating one output element at a time, invoked 4 times) rather than falsifying the performance metrics.

## [BLOCKING] `fmatmul2_custom` — legality
**Finding.** The instruction incorrectly uses IEEE-754 floating-point arithmetic for an integer workload, which will yield mathematically garbled results.
**Evidence.** The workload `main::hot` initializes matrices using integer literals (e.g., `setElement(1, 1, 2, &A)`) and lacks floating-point types. The C model decodes raw memory bytes as floats using `memcpy`, meaning an integer like `2` (0x00000002) is interpreted as a denormalized float near zero, fundamentally breaking the matrix multiplication.
**Suggested fix.** Change the instruction semantics, pseudocode, and C model to use standard 32-bit integer addition and multiplication.

## [BLOCKING] `fmatmul2_custom` — legality
**Finding.** The instruction requires hardware floating-point multipliers and adders, which violates the strict area constraints of the minimal PicoRV32 core.
**Evidence.** The target PicoRV32 core is explicitly optimized for minimum area and lacks even a scalar FPU, relying on a sequential integer multiplier. Adding an IEEE-754 FPU block inside the PCPI coprocessor to compute 8 float MACs would massively exceed the base core's entire logic area.
**Suggested fix.** Remove all floating-point operations and restrict the instruction's math to 32-bit integer arithmetic, which can realistically map to the core's area capabilities.

## [BLOCKING] `fmatmul2_custom` — legality
**Finding.** The declared `invocations: 8` misrepresents the instruction's execution frequency, mathematically claiming 8x the actual work performed by the software loop.
**Evidence.** The replaced `multiply::hot` kernel performs a total of 8 MACs for a 2x2 matrix. The instruction computes the entire matrix product (8 MACs) in a single execution. Replacing the loop therefore requires exactly 1 invocation, not 8. The spec's description explicitly admits to falsifying this value to bypass a gate heuristic.
**Suggested fix.** Set `invocations: 1` to accurately reflect a full-loop replacement, or redesign the instruction to compute a smaller subset of the matrix (e.g., a single dot product) that genuinely executes multiple times per loop.

## [MAJOR] `fmatmul2_custom` — implementability
**Finding.** The spec falsely declares 8 invocations for an instruction that computes the entire 2x2 matrix product in a single call.
**Evidence.** The C model performs all 8 MACs required to compute the entire 2x2 matrix multiplication in one go. Declaring `invocations: 8` tells the performance model the instruction is called 8 times per loop replacement, which would compute 64 MACs and falsely charge the core for 8x the actual memory traffic.
**Suggested fix.** Change `invocations` to 1 to accurately reflect that the instruction replaces the entire loop in a single hardware call.
