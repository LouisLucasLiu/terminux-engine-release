# third_party/libvterm/CMakeLists.txt
#
# Vendored libvterm（neovim fork v0.3.3）的 CMake 包裝。
# 不依賴 upstream Makefile / autoconf。產出靜態庫 target: vterm_static。

set(LIBVTERM_SOURCES
    src/encoding.c
    src/keyboard.c
    src/mouse.c
    src/parser.c
    src/pen.c
    src/screen.c
    src/state.c
    src/unicode.c
    src/vterm.c
)

add_library(vterm_static STATIC ${LIBVTERM_SOURCES})

target_include_directories(vterm_static
    PUBLIC  ${CMAKE_CURRENT_SOURCE_DIR}/include
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
)

# 對齊 upstream Makefile：-std=c99 -Wall -Wpedantic
# 使用 generator expression 限定 -std=c99 只對 C 檔（避免污染 AUTOMOC stub 等 C++ 編譯）
target_compile_options(vterm_static PRIVATE
    $<$<COMPILE_LANGUAGE:C>:-std=c99>
    -Wall
    -Wpedantic
    -Wno-unused-parameter   # libvterm 內部 callback 慣用，避免本 repo build 雜訊
)

set_target_properties(vterm_static PROPERTIES
    LINKER_LANGUAGE          C
    POSITION_INDEPENDENT_CODE ON
    AUTOMOC                  OFF   # 純 C library，無 Q_OBJECT，省掉 stub 編譯
    AUTORCC                  OFF
    AUTOUIC                  OFF
)
