#pragma once

#include <cmath>

// PS2Recomp currently uses unqualified isnan() in several
// generated-runtime FPU macros. GCC exposes the C++ overloads
// as std::isnan().
#if defined(__GNUC__) && !defined(_WIN32)
using std::isnan;
#endif
