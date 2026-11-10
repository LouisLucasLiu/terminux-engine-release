# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 Liu, Hsien-Ming (Louis Liu)
cmake_minimum_required(VERSION 3.16)
set(TERMINUX_VERSION_FILE "${CMAKE_CURRENT_SOURCE_DIR}/engine/src/terminux_engine/_version.py")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${TERMINUX_VERSION_FILE}")
file(STRINGS "${TERMINUX_VERSION_FILE}" TERMINUX_VERSION_DECLARATION
    REGEX "^__version__ = \"[0-9]+\\.[0-9]+\\.[0-9]+\"$")
if(NOT TERMINUX_VERSION_DECLARATION)
    message(FATAL_ERROR "無法從 _version.py 解析 Terminux 版本")
endif()
string(REGEX REPLACE "^__version__ = \"([0-9]+\\.[0-9]+\\.[0-9]+)\"$" "\\1"
    TERMINUX_VERSION_STRING "${TERMINUX_VERSION_DECLARATION}")
project(terminux_engine VERSION ${TERMINUX_VERSION_STRING} LANGUAGES C CXX)
add_compile_definitions(TERMINUX_VERSION="${TERMINUX_VERSION_STRING}")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

# ── Qt 6.7 以上 ──────────────────────────────────────────────────────────────────
# 發行建置由 -DCMAKE_PREFIX_PATH 指定 Qt SDK；不依賴開發者 HOME 位置。
find_package(Qt6 6.7 REQUIRED COMPONENTS Core Network Widgets Qml Quick QuickControls2)

option(TERMINUX_EXTERNAL_QT "使用安裝 prefix 下 qt symlink 指向的外部 Qt，不部署 Qt" OFF)
if(TERMINUX_EXTERNAL_QT)
    set(terminux_install_rpath "$ORIGIN/../qt/lib")
    file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/external-qt.conf"
        CONTENT "[Paths]\nPrefix=../qt\n")
    install(FILES "${CMAKE_CURRENT_BINARY_DIR}/external-qt.conf" DESTINATION bin RENAME qt.conf)
else()
    set(terminux_install_rpath "$ORIGIN/../lib")
endif()

# ── 選用元件：測試與內部 target ─────────────────────────────────────────────────
# 預設值依必要來源是否存在決定：來源齊全時預設 ON，缺少時預設 OFF。
# 可用 -D 明確指定；明確指定 ON 但來源不存在時 configure 失敗，不默默略過。

function(terminux_optional_component option_name source_path description)
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/${source_path}")
        set(default_value ON)
    else()
        set(default_value OFF)
    endif()
    option(${option_name} "${description}" ${default_value})
    if(${option_name} AND NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/${source_path}")
        message(FATAL_ERROR "${option_name}=ON，但找不到必要來源：${source_path}")
    endif()
endfunction()

terminux_optional_component(TERMINUX_BUILD_TESTS "cmake/terminux_tests.cmake" "建置 C++ 測試與 ctest 項目")
terminux_optional_component(TERMINUX_BUILD_INTERNAL_TARGETS "cmake/terminux_internal_targets.cmake"
    "建置內部使用、不安裝的 target")

# ── libvterm (vendored, neovim fork v0.3.3, MIT) ─────────────────────────────

add_subdirectory(third_party/libvterm)

# ── ipc_client (static library) ───────────────────────────────────────────────

add_library(ipc_client STATIC
    src/ipc/ipc_client.cpp
    src/ipc/agent_socket_path.cpp
    src/ipc/project_env.cpp
    src/ipc/jsonrpc_client.cpp
)
target_include_directories(ipc_client PUBLIC src)
target_link_libraries(ipc_client PUBLIC Qt6::Core Qt6::Network)

# ── notification_bar（終端機唯一的通知層）────────────────────────────────────

add_library(notification_bar STATIC
    src/terminal/notification_bar.cpp
)
target_include_directories(notification_bar PUBLIC src)
target_link_libraries(notification_bar PUBLIC
    Qt6::Core Qt6::Widgets
)

# ── pty_manager (forkpty + QSocketNotifier) ──────────────────────────────────

add_library(pty_manager STATIC src/terminal/pty_manager.cpp)
target_include_directories(pty_manager PUBLIC src)
target_link_libraries(pty_manager
    PUBLIC  Qt6::Core
    PRIVATE util   # forkpty (glibc, libutil)
)

# ── vterm_bridge (libvterm adapter, no PTY / no rendering) ───────────────────

add_library(vterm_bridge STATIC src/terminal/vterm_bridge.cpp)
target_include_directories(vterm_bridge PUBLIC src)
target_link_libraries(vterm_bridge PUBLIC Qt6::Core vterm_static)

# ── terminal_surface (QPainter cell grid, 鍵盤 → keyInput signal) ────────────

add_library(terminal_surface STATIC src/terminal/terminal_surface.cpp)
target_include_directories(terminal_surface PUBLIC src)
target_link_libraries(terminal_surface PUBLIC
    Qt6::Core Qt6::Widgets Qt6::Gui
    vterm_bridge
)

# ── terminux_terminal (composite) ────────────────────────────────────────────

add_library(terminux_terminal STATIC
    src/terminal/terminux_terminal.cpp
    src/terminal/chat_query_message.cpp   # chat query 回應 → 顯示文案
)
target_include_directories(terminux_terminal PUBLIC src)
target_link_libraries(terminux_terminal PUBLIC
    Qt6::Core Qt6::Widgets
    pty_manager
    vterm_bridge
    terminal_surface
    ipc_client          # lineage.pre_exec via JsonRpcClient
    notification_bar    # NotificationBar overlay
)

# ── terminux (application) ────────────────────────────────────────────────────

add_executable(terminux
    src/main.cpp
    src/terminal/main_window.cpp
    src/terminal/work_dock.cpp
    src/terminal/work_dock.h
)
install(TARGETS terminux RUNTIME DESTINATION bin)
if(NOT TARGET Qt6::QXcbIntegrationPlugin OR NOT TARGET Qt6::QOffscreenIntegrationPlugin)
    message(FATAL_ERROR "Terminux requires Qt xcb and offscreen platform plugins for deployment")
endif()
if(NOT TERMINUX_EXTERNAL_QT)
    # Qt 6.7 的 deploy script 會載入 <target>-plugins.cmake；在此明列首版
    # Linux xcb 與離線測試 offscreen plugin，並讓 Qt 的 deploy helper 一起處理依賴。
    file(GENERATE
        OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/.qt/terminux-plugins.cmake"
        CONTENT "set(__QT_DEPLOY_PLUGINS\n  \"$<TARGET_FILE:Qt6::QXcbIntegrationPlugin>\"\n  \"$<TARGET_FILE:Qt6::QOffscreenIntegrationPlugin>\"\n)\n"
    )
    qt_generate_deploy_app_script(
        TARGET terminux
        OUTPUT_SCRIPT terminux_deploy_script
        NO_TRANSLATIONS
    )
    install(SCRIPT ${terminux_deploy_script})
endif()
target_include_directories(terminux PRIVATE src)
target_link_libraries(terminux PRIVATE
    ipc_client
    terminux_terminal
    notification_bar
    Qt6::Core
    Qt6::Widgets
    Qt6::Network
)
set_target_properties(terminux PROPERTIES
    INSTALL_RPATH "${terminux_install_rpath}" INSTALL_RPATH_USE_LINK_PATH FALSE)

# ── QML-safe repository 與 private production transport 層 ─────────

add_library(dqe_repository STATIC
    src/dqe_viewer/dqe_viewer_client.cpp
    src/dqe_viewer/production/dqe_repository_controller.cpp
    src/dqe_viewer/production/opaque_id_registry.cpp
    src/dqe_viewer/production/archived_dto_mapper.cpp
    src/dqe_viewer/production/temporary_dto_mapper.cpp
    src/dqe_viewer/production/attachment_dto_mapper.cpp
    src/dqe_viewer/production/checkin_dto_mapper.cpp
    src/dqe_viewer/production/retention_dto_mapper.cpp
    src/dqe_viewer/production/purge_dto_mapper.cpp
    src/dqe_viewer/production/compare_snapshot_aggregator.cpp
    src/dqe_viewer/production/compare_html_exporter.cpp
    src/dqe_viewer/production/dqe_preview_image_provider.cpp
    src/dqe_viewer/production/dqe_operation_controller.cpp
    src/dqe_viewer/production/dqe_rpc_worker.cpp
    src/dqe_viewer/production/dqe_viewer_transport.cpp
)
target_include_directories(dqe_repository PUBLIC src)
target_link_libraries(dqe_repository PUBLIC ipc_client Qt6::Core Qt6::Gui Qt6::Network Qt6::Quick)

# ── DQE Viewer（QML 版）───────────────────────────────────────────────────────
# 不連 ipc_client、不連 Qt Widgets；QML 資源以 qt_add_qml_module（URI DqeViewer）註冊。
# QT_RESOURCE_ALIAS 把 src/dqe_viewer/qml/ 底下的檔案映射到模組根，讓
# DqeViewerApp.qml 的 `import "theme"` 能解析到手寫的 theme/qmldir singleton。

qt_add_executable(dqe_viewer
    src/dqe_viewer/qml_main.cpp
)
set_source_files_properties(src/dqe_viewer/qml/DqeViewerApp.qml PROPERTIES
    QT_RESOURCE_ALIAS DqeViewerApp.qml
)
set_source_files_properties(src/dqe_viewer/qml/theme/qmldir PROPERTIES
    QT_RESOURCE_ALIAS theme/qmldir
)
set_source_files_properties(src/dqe_viewer/qml/theme/DqeTheme.qml PROPERTIES
    QT_RESOURCE_ALIAS theme/DqeTheme.qml
)
# i18n：手寫 qmldir singleton（DqeI18n），供 `import "i18n"` 解析。
set_source_files_properties(src/dqe_viewer/qml/i18n/qmldir PROPERTIES
    QT_RESOURCE_ALIAS i18n/qmldir
)
set_source_files_properties(src/dqe_viewer/qml/i18n/DqeI18n.qml PROPERTIES
    QT_RESOURCE_ALIAS i18n/DqeI18n.qml
)
# shell chrome 元件（Header / Workspace Tabs / Status Bar）。
set_source_files_properties(src/dqe_viewer/qml/shell/DqeHeader.qml PROPERTIES
    QT_RESOURCE_ALIAS shell/DqeHeader.qml
)
set_source_files_properties(src/dqe_viewer/qml/shell/WorkspaceTabs.qml PROPERTIES
    QT_RESOURCE_ALIAS shell/WorkspaceTabs.qml
)
set_source_files_properties(src/dqe_viewer/qml/shell/DqeStatusBar.qml PROPERTIES
    QT_RESOURCE_ALIAS shell/DqeStatusBar.qml
)
# nested SplitView shell + per-workspace 持久化 + workspace placeholder。
set_source_files_properties(src/dqe_viewer/qml/shell/DqeWorkspaceShell.qml PROPERTIES
    QT_RESOURCE_ALIAS shell/DqeWorkspaceShell.qml
)
set_source_files_properties(src/dqe_viewer/qml/shell/LayoutSettings.qml PROPERTIES
    QT_RESOURCE_ALIAS shell/LayoutSettings.qml
)
set_source_files_properties(src/dqe_viewer/qml/workspaces/MockRegion.qml PROPERTIES
    QT_RESOURCE_ALIAS workspaces/MockRegion.qml
)
# 固定 mock repository 與 scenario fixtures（純 QML、無後端存取）。
set_source_files_properties(src/dqe_viewer/qml/mock/MockDqeRepository.qml PROPERTIES
    QT_RESOURCE_ALIAS mock/MockDqeRepository.qml
)
set_source_files_properties(src/dqe_viewer/qml/mock/MockArchivedData.qml PROPERTIES
    QT_RESOURCE_ALIAS mock/MockArchivedData.qml
)
set_source_files_properties(src/dqe_viewer/qml/mock/MockTemporaryData.qml PROPERTIES
    QT_RESOURCE_ALIAS mock/MockTemporaryData.qml
)
set_source_files_properties(src/dqe_viewer/qml/mock/MockCompareData.qml PROPERTIES
    QT_RESOURCE_ALIAS mock/MockCompareData.qml
)
# 共用 Summary Cards（只發送 status filter signal）。
set_source_files_properties(src/dqe_viewer/qml/review/DqeSummaryCards.qml PROPERTIES
    QT_RESOURCE_ALIAS review/DqeSummaryCards.qml
)
# DQE Item Table（篩選、排序與選取皆為唯讀 view 行為）。
set_source_files_properties(src/dqe_viewer/qml/review/DqeItemTable.qml PROPERTIES
    QT_RESOURCE_ALIAS review/DqeItemTable.qml
)
# DQE Item Inspector（只讀 item／attachment metadata）。
set_source_files_properties(src/dqe_viewer/qml/review/DqeItemInspector.qml PROPERTIES
    QT_RESOURCE_ALIAS review/DqeItemInspector.qml
)
# Evidence／Attachment list／safe preview 與三種明確 state。
set_source_files_properties(src/dqe_viewer/qml/review/EvidencePanel.qml PROPERTIES
    QT_RESOURCE_ALIAS review/EvidencePanel.qml
)
set_source_files_properties(src/dqe_viewer/qml/review/AttachmentList.qml PROPERTIES
    QT_RESOURCE_ALIAS review/AttachmentList.qml
)
set_source_files_properties(src/dqe_viewer/qml/review/AttachmentPreview.qml PROPERTIES
    QT_RESOURCE_ALIAS review/AttachmentPreview.qml
)
set_source_files_properties(src/dqe_viewer/qml/review/AttachmentPreviewDialog.qml PROPERTIES
    QT_RESOURCE_ALIAS review/AttachmentPreviewDialog.qml
)
set_source_files_properties(src/dqe_viewer/qml/states/UnsupportedAttachmentState.qml PROPERTIES
    QT_RESOURCE_ALIAS states/UnsupportedAttachmentState.qml
)
set_source_files_properties(src/dqe_viewer/qml/states/MissingAttachmentState.qml PROPERTIES
    QT_RESOURCE_ALIAS states/MissingAttachmentState.qml
)
set_source_files_properties(src/dqe_viewer/qml/states/IntegrityIncompleteState.qml PROPERTIES
    QT_RESOURCE_ALIAS states/IntegrityIncompleteState.qml
)
# canonical state marker/message 與 workspace state overlay。
set_source_files_properties(src/dqe_viewer/qml/states/StateMessage.qml PROPERTIES
    QT_RESOURCE_ALIAS states/StateMessage.qml
)
set_source_files_properties(src/dqe_viewer/qml/states/WorkspaceState.qml PROPERTIES
    QT_RESOURCE_ALIAS states/WorkspaceState.qml
)
# Archived workspace composition 與固定 mock source navigator。
set_source_files_properties(src/dqe_viewer/qml/workspaces/ArchivedWorkspace.qml PROPERTIES
    QT_RESOURCE_ALIAS workspaces/ArchivedWorkspace.qml
)
set_source_files_properties(src/dqe_viewer/qml/navigation/ArchivedVersionNavigator.qml PROPERTIES
    QT_RESOURCE_ALIAS navigation/ArchivedVersionNavigator.qml
)

# Temporary workspace composition、mock job navigator、mock lifecycle 動作列與資料。
set_source_files_properties(src/dqe_viewer/qml/workspaces/TemporaryWorkspace.qml PROPERTIES
    QT_RESOURCE_ALIAS workspaces/TemporaryWorkspace.qml
)
set_source_files_properties(src/dqe_viewer/qml/navigation/TemporaryJobNavigator.qml PROPERTIES
    QT_RESOURCE_ALIAS navigation/TemporaryJobNavigator.qml
)
set_source_files_properties(src/dqe_viewer/qml/review/LifecycleActionBar.qml PROPERTIES
    QT_RESOURCE_ALIAS review/LifecycleActionBar.qml
)
set_source_files_properties(src/dqe_viewer/qml/mock/MockTemporaryLifecycle.qml PROPERTIES
    QT_RESOURCE_ALIAS mock/MockTemporaryLifecycle.qml
)
# 純資料 Compare view model 與 first-point 正規化 helper（無 chart／後端）。
set_source_files_properties(src/dqe_viewer/qml/compare/CompareViewModel.qml PROPERTIES
    QT_RESOURCE_ALIAS compare/CompareViewModel.qml
)
set_source_files_properties(src/dqe_viewer/qml/compare/Normalization.js PROPERTIES
    QT_RESOURCE_ALIAS compare/Normalization.js
)

# Compare workspace visualization（NormalizedTrendChart 使用 Qt Quick Shapes）。
set_source_files_properties(src/dqe_viewer/qml/workspaces/CompareWorkspace.qml PROPERTIES
    QT_RESOURCE_ALIAS workspaces/CompareWorkspace.qml
)
set_source_files_properties(src/dqe_viewer/qml/navigation/CompareSourceSelector.qml PROPERTIES
    QT_RESOURCE_ALIAS navigation/CompareSourceSelector.qml
)
set_source_files_properties(src/dqe_viewer/qml/compare/NormalizedTrendChart.qml PROPERTIES
    QT_RESOURCE_ALIAS compare/NormalizedTrendChart.qml
)
set_source_files_properties(src/dqe_viewer/qml/compare/RawComparisonTable.qml PROPERTIES
    QT_RESOURCE_ALIAS compare/RawComparisonTable.qml
)
set_source_files_properties(src/dqe_viewer/qml/compare/TrendPointInspector.qml PROPERTIES
    QT_RESOURCE_ALIAS compare/TrendPointInspector.qml
)
set_source_files_properties(src/dqe_viewer/qml/mock/MockCompareCatalog.qml PROPERTIES
    QT_RESOURCE_ALIAS mock/MockCompareCatalog.qml
)
# 唯一 repository provider surface；production context 只由啟動參數注入。
set_source_files_properties(src/dqe_viewer/qml/repository/DqeRepositoryFacade.qml PROPERTIES
    QT_RESOURCE_ALIAS repository/DqeRepositoryFacade.qml
)

# RESOURCE_PREFIX：讓 module 的 qmldir 位於預設 import path（qrc:/qt/qml）。否則
# 安裝後找不到 DqeViewer module（build tree 只是剛好在執行檔旁有 DqeViewer/ 目錄）。
qt_add_qml_module(dqe_viewer
    URI DqeViewer
    VERSION 1.0
    RESOURCE_PREFIX /qt/qml
    QML_FILES
        src/dqe_viewer/qml/DqeViewerApp.qml
        src/dqe_viewer/qml/shell/DqeHeader.qml
        src/dqe_viewer/qml/shell/WorkspaceTabs.qml
        src/dqe_viewer/qml/shell/DqeStatusBar.qml
        src/dqe_viewer/qml/shell/DqeWorkspaceShell.qml
        src/dqe_viewer/qml/shell/LayoutSettings.qml
        src/dqe_viewer/qml/workspaces/MockRegion.qml
        src/dqe_viewer/qml/workspaces/ArchivedWorkspace.qml
        src/dqe_viewer/qml/navigation/ArchivedVersionNavigator.qml
        src/dqe_viewer/qml/workspaces/TemporaryWorkspace.qml
        src/dqe_viewer/qml/navigation/TemporaryJobNavigator.qml
        src/dqe_viewer/qml/review/LifecycleActionBar.qml
        src/dqe_viewer/qml/mock/MockTemporaryLifecycle.qml
        src/dqe_viewer/qml/compare/CompareViewModel.qml
        src/dqe_viewer/qml/compare/Normalization.js
        src/dqe_viewer/qml/workspaces/CompareWorkspace.qml
        src/dqe_viewer/qml/navigation/CompareSourceSelector.qml
        src/dqe_viewer/qml/compare/NormalizedTrendChart.qml
        src/dqe_viewer/qml/compare/RawComparisonTable.qml
        src/dqe_viewer/qml/compare/TrendPointInspector.qml
        src/dqe_viewer/qml/mock/MockCompareCatalog.qml
        src/dqe_viewer/qml/repository/DqeRepositoryFacade.qml
        src/dqe_viewer/qml/mock/MockDqeRepository.qml
        src/dqe_viewer/qml/mock/MockArchivedData.qml
        src/dqe_viewer/qml/mock/MockTemporaryData.qml
        src/dqe_viewer/qml/mock/MockCompareData.qml
        src/dqe_viewer/qml/review/DqeSummaryCards.qml
        src/dqe_viewer/qml/review/DqeItemTable.qml
        src/dqe_viewer/qml/review/DqeItemInspector.qml
        src/dqe_viewer/qml/review/EvidencePanel.qml
        src/dqe_viewer/qml/review/AttachmentList.qml
        src/dqe_viewer/qml/review/AttachmentPreview.qml
        src/dqe_viewer/qml/review/AttachmentPreviewDialog.qml
        src/dqe_viewer/qml/states/UnsupportedAttachmentState.qml
        src/dqe_viewer/qml/states/MissingAttachmentState.qml
        src/dqe_viewer/qml/states/IntegrityIncompleteState.qml
        src/dqe_viewer/qml/states/StateMessage.qml
        src/dqe_viewer/qml/states/WorkspaceState.qml
    RESOURCES
        src/dqe_viewer/qml/theme/qmldir
        src/dqe_viewer/qml/theme/DqeTheme.qml
        src/dqe_viewer/qml/i18n/qmldir
        src/dqe_viewer/qml/i18n/DqeI18n.qml
)
target_link_libraries(dqe_viewer PRIVATE
    dqe_repository
    Qt6::Core
    Qt6::Gui
    Qt6::Qml
    Qt6::Quick
    Qt6::QuickControls2
)

# 安裝 dqe_viewer。安裝到與 Python 環境相同的 prefix 時，位置就是
# `sys.prefix/bin/dqe_viewer`。QML deploy script 以 qmlimportscanner 部署用到的
# QML module 與 plugin；`qt_add_executable` 的 target 由 Qt 自行產生 plugin 清單
#（含 platform plugin）。
set_target_properties(dqe_viewer PROPERTIES
    INSTALL_RPATH "${terminux_install_rpath}" INSTALL_RPATH_USE_LINK_PATH FALSE)
install(TARGETS dqe_viewer RUNTIME DESTINATION bin)
if(NOT TERMINUX_EXTERNAL_QT)
    qt_generate_deploy_qml_app_script(
        TARGET dqe_viewer
        OUTPUT_SCRIPT dqe_viewer_deploy_script
        NO_TRANSLATIONS
    )
    install(SCRIPT ${dqe_viewer_deploy_script})
endif()

# ── 內部 target 與測試 ─────────────────────────────────────────────────────────

if(TERMINUX_BUILD_INTERNAL_TARGETS)
    include(cmake/terminux_internal_targets.cmake)
endif()

if(TERMINUX_BUILD_TESTS)
    include(cmake/terminux_tests.cmake)
endif()
