# libvterm (vendored)

Vendored from `https://github.com/neovim/libvterm`
- Upstream commit: `934bc2fbf21800ac3458a499df8820ca5fb45fd3`
- Upstream version: `0.3.3`
- Upstream license: MIT (Paul Evans `<leonerd@leonerd.org.uk>`)

## 內容

只保留 build 與使用所需檔案：

```
include/        vterm.h, vterm_keycodes.h（公開 API）
src/            *.c / *.h / fullwidth.inc（library 來源）
src/encoding/   DECdrawing.inc, uk.inc（編碼表）
LICENSE         MIT 授權聲明（保留）
CMakeLists.txt  本專案自製，定義 vterm_static target
```

未保留：upstream 的 `Makefile` / `bin/` / `t/`（測試）/ `doc/` / `find-wide-chars.pl` / `tbl2inc_c.pl`。

## 升級流程

```bash
cd /tmp && rm -rf libvterm-src
git clone --depth=1 https://github.com/neovim/libvterm.git libvterm-src
cp /tmp/libvterm-src/include/*.h third_party/libvterm/include/
cp /tmp/libvterm-src/src/*.c /tmp/libvterm-src/src/*.h /tmp/libvterm-src/src/*.inc third_party/libvterm/src/
cp /tmp/libvterm-src/src/encoding/*.inc third_party/libvterm/src/encoding/
cp /tmp/libvterm-src/LICENSE third_party/libvterm/
```

升級後更新本檔上方的 commit hash 與版本號。
