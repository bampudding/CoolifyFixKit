# Default budgets for future interactive/noninteractive build shells.
# Explicit caller budgets remain authoritative.
export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-4}"
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-4}"
export CTEST_PARALLEL_LEVEL="${CTEST_PARALLEL_LEVEL:-2}"
