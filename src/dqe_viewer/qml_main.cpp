// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// DQE Viewer production binding：QML DQE Viewer 入口。
//
// DQE Viewer production binding 邊界：CLI 先完整決定 mock 或 isolated staging production mode，才可以建立
// controller/worker。QML 只接收 safe controller surface，不讀 endpoint、path、DB 或 ref。
//
// Settings 身分：organization=terminux、application=dqe_viewer_qml，
// 與舊 Widgets viewer 的 QSettings 狀態隔離。

#include <QCommandLineOption>
#include <QCommandLineParser>
#include <QDir>
#include <QGuiApplication>
#include <QImage>
#include <QLibraryInfo>
#include <QQmlApplicationEngine>
#include <QQmlComponent>
#include <QQuickWindow>
#include <QTimer>
#include <QDebug>
#include <QObject>
#include <QPointer>
#include <QVariant>

#include <thread>

#include "ipc/jsonrpc_client.h"

#include "dqe_viewer/production/compare_html_exporter.h"
#include "dqe_viewer/production/compare_snapshot_aggregator.h"
#include "dqe_viewer/production/dqe_operation_controller.h"
#include "dqe_viewer/production/dqe_preview_image_provider.h"
#include "dqe_viewer/production/dqe_repository_controller.h"
#include "dqe_viewer/production/dqe_repository_types.h"
#include "dqe_viewer/production/dqe_viewer_transport.h"

#include <memory>
#include <cstdio>
#include <cstring>

namespace {

// 解析 "WxH" → QSize；失敗回傳無效 QSize。
QSize parseWindowSize(const QString &value)
{
    const int sep = value.indexOf(QLatin1Char('x'), 0, Qt::CaseInsensitive);
    if (sep <= 0 || sep >= value.size() - 1) {
        return QSize();
    }
    bool okW = false;
    bool okH = false;
    const int w = value.left(sep).toInt(&okW);
    const int h = value.mid(sep + 1).toInt(&okH);
    if (!okW || !okH || w <= 0 || h <= 0) {
        return QSize();
    }
    return QSize(w, h);
}

bool isKnownUiScenario(const QString &value)
{
    static const QStringList scenarios = {
        QStringLiteral("archived-main"),
        QStringLiteral("fail-selected"),
        QStringLiteral("attachment-maximized"),
        QStringLiteral("panels-resized"),
        QStringLiteral("temporary-empty"),
        QStringLiteral("database-unavailable"),
        QStringLiteral("compare-trend"),
    };
    return scenarios.contains(value);
}

int workspaceIndex(const QString &value)
{
    if (value == QLatin1String("archived")) {
        return 0;
    }
    if (value == QLatin1String("temporary")) {
        return 1;
    }
    if (value == QLatin1String("compare")) {
        return 2;
    }
    return -1;
}

bool isTerminalRepositoryState(const QString &state)
{
    return state == QLatin1String("ready") || state == QLatin1String("partial")
           || state == QLatin1String("empty") || state == QLatin1String("unavailable")
           || state == QLatin1String("error");
}

} // namespace

// 將 QML 的動態 signal 轉成型別安全的 C++ signal，供 screenshot path 等待 scenario ready。
class ScenarioReadyObserver final : public QObject
{
    Q_OBJECT

public:
    explicit ScenarioReadyObserver(QString expectedScenario)
        : m_expectedScenario(expectedScenario)
    {
    }

    bool isReady() const { return m_ready; }

public slots:
    void onScenarioReady(const QString &scenarioName)
    {
        if (m_ready || scenarioName != m_expectedScenario) {
            return;
        }
        m_ready = true;
        emit readyForCapture();
    }

signals:
    void readyForCapture();

private:
    QString m_expectedScenario;
    bool m_ready = false;
};

int main(int argc, char *argv[])
{
    // --version 後面有值時是 Archived 的顯示 context，由下方的 parser 處理；
    // 放在最後、沒有值時印出版本與授權資訊，格式與 terminux --version 相同。
    if (argc > 1 && std::strcmp(argv[argc - 1], "--version") == 0) {
        std::printf("Terminux %s\n"
                    "Copyright 2026 Liu, Hsien-Ming (Louis Liu)\n"
                    "License: PolyForm Shield 1.0.0 <https://polyformproject.org/licenses/shield/1.0.0/>\n",
                    TERMINUX_VERSION);
        return 0;
    }
    bool selfCheck = false;
    for (int index = 1; index < argc; ++index) {
        selfCheck = selfCheck || std::strcmp(argv[index], "--self-check") == 0;
    }
    if (selfCheck) {
        qputenv("QT_QPA_PLATFORM", "offscreen");
    }
    QGuiApplication app(argc, argv);
    if (selfCheck) {
        // 只載入與編譯正式 module，不建立 Window、Settings 或 transport。
        QQmlEngine engine;
        for (const auto *type : {"DqeViewerApp", "NormalizedTrendChart"}) {
            QQmlComponent component(&engine);
            component.loadFromModule(QStringLiteral("DqeViewer"), QString::fromLatin1(type));
            if (!component.isReady()) {
                qCritical().noquote() << QStringLiteral("Viewer 自我檢查失敗：%1\n%2")
                    .arg(QString::fromLatin1(type), component.errorString());
                return 2;
            }
        }
        qInfo().noquote() << QStringLiteral("Viewer 自我檢查通過；Qt %1；plugins=%2；qml=%3")
            .arg(QString::fromLatin1(qVersion()), QLibraryInfo::path(QLibraryInfo::PluginsPath),
                 QLibraryInfo::path(QLibraryInfo::QmlImportsPath));
        return 0;
    }
    // QSettings 身分（與舊 viewer 隔離）；視窗標題於 QML 內設為 "DQE Viewer"。
    QCoreApplication::setOrganizationName(QStringLiteral("terminux"));
    QCoreApplication::setApplicationName(QStringLiteral("dqe_viewer_qml"));

    QCommandLineParser parser;
    parser.setApplicationDescription(
        QStringLiteral("DQE Viewer. Normally launched by the Terminux daemon; without an Archived or "
                       "Temporary source it shows built-in sample data."));
    parser.addHelpOption();

    const QCommandLineOption windowSizeOption(
        QStringLiteral("window-size"),
        QStringLiteral("Window size as WxH, e.g. 1440x900."),
        QStringLiteral("WxH"));
    const QCommandLineOption screenshotOption(
        QStringLiteral("screenshot"),
        QStringLiteral("Capture a ready UI scenario to PNG <path>, then exit."),
        QStringLiteral("path"));
    const QCommandLineOption jobKeyOption(
        QStringLiteral("job-key"),
        QStringLiteral("Job key to focus in the Temporary workspace or the sample data."),
        QStringLiteral("job-key"));
    const QCommandLineOption smokeExitOption(
        QStringLiteral("smoke-exit"),
        QStringLiteral("Construct the QML root, validate it loaded, and exit."));
    // 只供 sample data／offscreen visual regression 使用，不列在 --help；未指定時不改變
    // 預設的 Archived workspace，也不影響 daemon 的 launcher。
    QCommandLineOption uiScenarioOption(
        QStringLiteral("ui-scenario"),
        QStringLiteral("Sample-data scenario for visual regression captures."),
        QStringLiteral("name"));
    uiScenarioOption.setFlags(QCommandLineOption::HiddenFromHelp);
    const QCommandLineOption ipcEndpointOption(
        QStringLiteral("ipc-endpoint"),
        QStringLiteral("Terminux daemon IPC endpoint (requires an Archived or Temporary source)."),
        QStringLiteral("endpoint"));
    const QCommandLineOption archivedVersionDirOption(
        QStringLiteral("archived-version-dir"),
        QStringLiteral("Archived version directory."), QStringLiteral("directory"));
    const QCommandLineOption temporaryDbPathOption(
        QStringLiteral("temporary-db-path"),
        QStringLiteral("Temporary DQE database."), QStringLiteral("database"));
    const QCommandLineOption temporaryRetentionDaysOption(
        QStringLiteral("temporary-retention-days"),
        QStringLiteral("Temporary retention policy context in days (1..3650)."), QStringLiteral("days"));
    const QCommandLineOption workspaceOption(
        QStringLiteral("workspace"),
        QStringLiteral("Initial workspace: archived, temporary, or compare."), QStringLiteral("workspace"));
    const QCommandLineOption versionOption(
        QStringLiteral("version"),
        QStringLiteral("Optional archived version display context. As the last argument without a "
                       "value, prints version information and exits."),
        QStringLiteral("version"));
    const QCommandLineOption dbOption(
        QStringLiteral("db"),
        QStringLiteral("Compatibility alias for --temporary-db-path only."), QStringLiteral("database"));
    const QCommandLineOption selectTabOption(
        QStringLiteral("select-tab"),
        QStringLiteral("Compatibility initial workspace: archived, temporary, or compare."),
        QStringLiteral("workspace"));

    parser.addOption(windowSizeOption);
    parser.addOption(screenshotOption);
    parser.addOption(jobKeyOption);
    parser.addOption(smokeExitOption);
    parser.addOption(uiScenarioOption);
    parser.addOption(ipcEndpointOption);
    parser.addOption(archivedVersionDirOption);
    parser.addOption(temporaryDbPathOption);
    parser.addOption(temporaryRetentionDaysOption);
    parser.addOption(workspaceOption);
    parser.addOption(versionOption);
    parser.addOption(dbOption);
    parser.addOption(selectTabOption);
    parser.process(app);

    QSize windowSize;
    if (parser.isSet(windowSizeOption)) {
        windowSize = parseWindowSize(parser.value(windowSizeOption));
        if (!windowSize.isValid()) {
            qCritical().noquote() << QStringLiteral("invalid --window-size (expected WxH, e.g. 1440x900)");
            return 1;
        }
    }

    const QString uiScenario = parser.value(uiScenarioOption);
    if (parser.isSet(uiScenarioOption) && !isKnownUiScenario(uiScenario)) {
        qCritical().noquote() << QStringLiteral("unknown --ui-scenario");
        return 1;
    }

    const bool hasEndpoint = parser.isSet(ipcEndpointOption);
    const bool hasArchived = parser.isSet(archivedVersionDirOption);
    const bool hasTemporaryPath = parser.isSet(temporaryDbPathOption);
    const bool hasDbAlias = parser.isSet(dbOption);
    const bool hasTemporary = hasTemporaryPath || hasDbAlias;
    const bool hasRetention = parser.isSet(temporaryRetentionDaysOption);
    const bool hasVersion = parser.isSet(versionOption);
    const bool hasSourceContext = hasArchived || hasTemporary;
    const bool hasProductionFlag = hasEndpoint || hasSourceContext || hasRetention || hasVersion;

    const auto reject = [](const QString &message) {
        qCritical().noquote() << message;
        return 1;
    };
    if (parser.isSet(uiScenarioOption) && hasProductionFlag) {
        return reject(QStringLiteral("--ui-scenario cannot be combined with production context"));
    }
    if (hasEndpoint && parser.value(ipcEndpointOption).isEmpty()) {
        return reject(QStringLiteral("--ipc-endpoint requires a value"));
    }
    if (hasArchived && parser.value(archivedVersionDirOption).isEmpty()) {
        return reject(QStringLiteral("--archived-version-dir requires a value"));
    }
    if (hasTemporaryPath && parser.value(temporaryDbPathOption).isEmpty()) {
        return reject(QStringLiteral("--temporary-db-path requires a value"));
    }
    if (hasDbAlias && parser.value(dbOption).isEmpty()) {
        return reject(QStringLiteral("--db requires a value"));
    }
    if (hasTemporaryPath && hasDbAlias
        && parser.value(temporaryDbPathOption) != parser.value(dbOption)) {
        return reject(QStringLiteral("--db conflicts with --temporary-db-path"));
    }
    if (hasRetention && !hasTemporary) {
        return reject(QStringLiteral("--temporary-retention-days requires a Temporary source context"));
    }
    if (hasVersion && !hasArchived) {
        return reject(QStringLiteral("--version requires --archived-version-dir"));
    }
    if (hasSourceContext && !hasEndpoint) {
        return reject(QStringLiteral("production source context requires --ipc-endpoint"));
    }
    if (hasEndpoint && !hasSourceContext) {
        return reject(QStringLiteral("--ipc-endpoint requires an Archived or Temporary source context"));
    }

    int retentionDays = 14;
    if (hasRetention) {
        bool retentionOk = false;
        retentionDays = parser.value(temporaryRetentionDaysOption).toInt(&retentionOk);
        if (!retentionOk || retentionDays < 1 || retentionDays > 3650) {
            return reject(QStringLiteral("invalid --temporary-retention-days (expected 1..3650)"));
        }
    }

    QString requestedWorkspace;
    if (parser.isSet(workspaceOption)) {
        requestedWorkspace = parser.value(workspaceOption);
        if (workspaceIndex(requestedWorkspace) < 0) {
            return reject(QStringLiteral("invalid --workspace (expected archived, temporary, or compare)"));
        }
    }
    if (parser.isSet(selectTabOption)) {
        const QString tab = parser.value(selectTabOption);
        if (workspaceIndex(tab) < 0) {
            return reject(QStringLiteral("invalid --select-tab (expected archived, temporary, or compare)"));
        }
        if (!requestedWorkspace.isEmpty() && requestedWorkspace != tab) {
            return reject(QStringLiteral("--workspace conflicts with --select-tab"));
        }
        requestedWorkspace = tab;
    }

    const bool productionMode = hasEndpoint && hasSourceContext;
    if (productionMode && parser.isSet(jobKeyOption) && !hasTemporary) {
        return reject(QStringLiteral("--job-key requires a Temporary source context in production mode"));
    }
    if (productionMode && requestedWorkspace == QLatin1String("archived") && !hasArchived) {
        return reject(QStringLiteral("--workspace selects unavailable Archived context"));
    }
    if (productionMode && requestedWorkspace == QLatin1String("temporary") && !hasTemporary) {
        return reject(QStringLiteral("--workspace selects unavailable Temporary context"));
    }
    if (parser.isSet(screenshotOption) && !parser.isSet(uiScenarioOption) && !productionMode) {
        return reject(QStringLiteral("--screenshot requires a known --ui-scenario or production context"));
    }

    // 所有 CLI 組合到此處已完成驗證。以下才可建立 worker transport；constructor 不發 RPC，
    // 實際 read 仍由 QML workspace 的 refresh 排入 worker thread。
    const QString temporaryDbPath = hasTemporaryPath ? parser.value(temporaryDbPathOption)
                                                      : parser.value(dbOption);
    const QString endpoint = parser.value(ipcEndpointOption);
    int initialWorkspace = 0;
    if (!requestedWorkspace.isEmpty()) {
        initialWorkspace = workspaceIndex(requestedWorkspace);
    } else if (productionMode && !hasArchived) {
        initialWorkspace = 1;
    }

    std::unique_ptr<terminux::dqe::production::DqeRepositoryController> archivedController;
    std::unique_ptr<terminux::dqe::production::DqeRepositoryController> temporaryController;
    std::unique_ptr<terminux::dqe::production::DqeOperationController> temporaryOperationController;
    std::unique_ptr<terminux::dqe::production::CompareSnapshotAggregator> compareAggregator;
    if (productionMode) {
        if (hasArchived) {
            terminux::dqe::production::ArchivedLaunchContext context;
            context.ipcEndpoint = endpoint;
            context.archivedVersionDir = parser.value(archivedVersionDirOption);
            context.workspaceDisplayContext = requestedWorkspace;
            context.versionDisplayContext = parser.value(versionOption);
            archivedController = std::make_unique<terminux::dqe::production::DqeRepositoryController>(
                std::make_unique<terminux::dqe::production::DqeViewerTransport>(context));
        }
        if (hasTemporary) {
            terminux::dqe::production::TemporaryLaunchContext context;
            context.ipcEndpoint = endpoint;
            context.temporaryDbPath = temporaryDbPath;
            context.retentionDays = retentionDays;
            context.jobKeyToOpen = parser.value(jobKeyOption);
            temporaryController = std::make_unique<terminux::dqe::production::DqeRepositoryController>(
                std::make_unique<terminux::dqe::production::DqeViewerTransport>(context));
            // operation controller 使用獨立 worker transport；staging smoke 只觸發 read，不會
            // 呼叫 Check-in、Retention 或 Purge。
            temporaryOperationController =
                std::make_unique<terminux::dqe::production::DqeOperationController>(
                    temporaryController.get(),
                    std::make_unique<terminux::dqe::production::DqeViewerTransport>(context));
        }
        compareAggregator =
            std::make_unique<terminux::dqe::production::CompareSnapshotAggregator>();
        if (archivedController) {
            compareAggregator->addRepository(archivedController.get());
        }
        if (temporaryController) {
            compareAggregator->addRepository(temporaryController.get());
        }
    }

    // Compare HTML 匯出 helper：mock 與 production 都提供（QML 無法自行寫檔）。
    auto compareHtmlExporter = std::make_unique<terminux::dqe::production::CompareHtmlExporter>();

    QVariantMap initialProperties;
    initialProperties.insert(QStringLiteral("jobKey"), parser.value(jobKeyOption));
    // 啟動語言之後才由背景 thread 查 daemon 注入（見 app.exec() 前），不阻塞啟動；
    // 此處留空，QML 端先用 DqeI18n 預設。
    initialProperties.insert(QStringLiteral("initialLanguage"), QString());
    initialProperties.insert(QStringLiteral("activeWorkspace"), initialWorkspace);
    initialProperties.insert(QStringLiteral("compareHtmlExporter"),
                             QVariant::fromValue(static_cast<QObject *>(compareHtmlExporter.get())));
    if (productionMode) {
        initialProperties.insert(QStringLiteral("productionMode"), true);
        initialProperties.insert(QStringLiteral("archivedRepositoryController"),
                                 QVariant::fromValue(static_cast<QObject *>(archivedController.get())));
        initialProperties.insert(QStringLiteral("temporaryRepositoryController"),
                                 QVariant::fromValue(static_cast<QObject *>(temporaryController.get())));
        initialProperties.insert(QStringLiteral("temporaryOperationController"),
                                 QVariant::fromValue(static_cast<QObject *>(temporaryOperationController.get())));
        initialProperties.insert(QStringLiteral("compareSnapshotProvider"),
                                 QVariant::fromValue(static_cast<QObject *>(compareAggregator.get())));
    }

    QQmlApplicationEngine engine;
    engine.setInitialProperties(initialProperties);
    // production image preview 只經 opaque memory token 讀取，沒有暫存檔。
    engine.addImageProvider(QStringLiteral("dqe-preview"),
                            new terminux::dqe::production::DqePreviewImageProvider);
    engine.loadFromModule("DqeViewer", "DqeViewerApp");
    if (engine.rootObjects().isEmpty()) {
        qCritical().noquote() << QStringLiteral("failed to load QML root DqeViewer/DqeViewerApp");
        return 2;
    }

    auto *window = qobject_cast<QQuickWindow *>(engine.rootObjects().constFirst());
    if (window == nullptr) {
        qCritical().noquote() << QStringLiteral("QML root is not a window");
        return 2;
    }

    if (parser.isSet(jobKeyOption)) {
        window->setProperty("jobKey", parser.value(jobKeyOption));
    }
    // setInitialProperties 的 map iteration 不作 UI precedence contract；在 job-key handler 後
    // 明確重設已驗證的 workspace，避免 job-key 暗中切換 mode 或 tab。
    window->setProperty("activeWorkspace", initialWorkspace);
    if (windowSize.isValid()) {
        window->resize(windowSize);
    }
    if (productionMode) {
        // DQE Viewer production binding staging startup 唯一允許的自動 work 是已驗證 source context 的 read list。
        // 不呼叫 load_attachment、Check-in、Retention 或 Purge；invalid CLI 也絕不會到達此處。
        if (archivedController) {
            archivedController->requestSources();
        }
        if (temporaryController) {
            temporaryController->requestSources();
        }
    }

    // mock smoke 只驗證 QML root；staging smoke 額外等待 read state 終態，仍絕不啟動
    // lifecycle operation。這讓 malformed CLI（進不到這裡）與 source unavailable（安全 UI
    // 終態）是不同階段。
    if (parser.isSet(smokeExitOption) && !parser.isSet(screenshotOption)) {
        if (!productionMode) {
            return 0;
        }
        auto done = std::make_shared<bool>(false);
        QTimer readyPoll;
        readyPoll.setInterval(20);
        QTimer timeout;
        timeout.setSingleShot(true);
        QObject::connect(&readyPoll, &QTimer::timeout, &app,
                         [&app, &readyPoll, done, &archivedController, &temporaryController]() {
            const bool archivedReady = !archivedController
                                       || isTerminalRepositoryState(archivedController->repositoryState());
            const bool temporaryReady = !temporaryController
                                        || isTerminalRepositoryState(temporaryController->repositoryState());
            if (*done || !archivedReady || !temporaryReady) {
                return;
            }
            *done = true;
            readyPoll.stop();
            app.exit(0);
        });
        QObject::connect(&timeout, &QTimer::timeout, &app, [&app, done]() {
            if (*done) {
                return;
            }
            *done = true;
            qCritical().noquote() << QStringLiteral("staging smoke timed out waiting for source state");
            app.exit(1);
        });
        readyPoll.start();
        timeout.start(5000);
        return app.exec();
    }

    // --screenshot：先等待 QML `uiScenarioReady`（workspace、state、chart/preview 都已
    // 完成），再等待下一個 frameSwapped 擷取；絕不以第一個 frame 或固定 sleep 當 ready。
    if (parser.isSet(screenshotOption)) {
        const QString path = parser.value(screenshotOption);
        if (productionMode) {
            // staging screenshot 等候既有 read controller 的終態；不需要、也不允許 mock
            // uiScenario handshake。這個流程只觀察 list/read state，不呼叫 lifecycle。
            auto done = std::make_shared<bool>(false);
            auto readyForCapture = std::make_shared<bool>(false);
            QTimer readyPoll;
            readyPoll.setInterval(20);
            QTimer timeout;
            timeout.setSingleShot(true);
            QObject::connect(&readyPoll, &QTimer::timeout, &app,
                             [window, &readyPoll, done, readyForCapture, &archivedController,
                              &temporaryController]() {
                const bool archivedReady = !archivedController
                                           || isTerminalRepositoryState(archivedController->repositoryState());
                const bool temporaryReady = !temporaryController
                                            || isTerminalRepositoryState(temporaryController->repositoryState());
                if (*done || !archivedReady || !temporaryReady) {
                    return;
                }
                *readyForCapture = true;
                readyPoll.stop();
                window->update();
            });
            QObject::connect(window, &QQuickWindow::frameSwapped, &app,
                             [window, path, &app, done, readyForCapture]() {
                if (*done || !*readyForCapture) {
                    return;
                }
                *done = true;
                const QImage image = window->grabWindow();
                if (image.isNull() || !image.save(path)) {
                    qCritical().noquote() << QStringLiteral("staging screenshot capture failed");
                    app.exit(1);
                    return;
                }
                app.exit(0);
            });
            QObject::connect(&timeout, &QTimer::timeout, &app, [&app, done]() {
                if (*done) {
                    return;
                }
                *done = true;
                qCritical().noquote()
                    << QStringLiteral("staging screenshot timed out waiting for source state");
                app.exit(1);
            });
            readyPoll.start();
            timeout.start(5000);
            return app.exec();
        }

        auto done = std::make_shared<bool>(false);
        ScenarioReadyObserver scenarioReady(uiScenario);

        if (window->metaObject()->indexOfProperty("uiScenario") < 0) {
            qCritical().noquote() << QStringLiteral("uiScenario property is unavailable on QML root");
            return 2;
        }

        if (!QObject::connect(window, SIGNAL(uiScenarioReady(QString)), &scenarioReady,
                              SLOT(onScenarioReady(QString)))) {
            qCritical().noquote() << QStringLiteral("uiScenarioReady signal is unavailable on QML root");
            return 2;
        }
        QObject::connect(&scenarioReady, &ScenarioReadyObserver::readyForCapture, window, [window]() {
            // 要求 scenario ready 後的一個新 frame，確保 grabWindow 不會抓到前一個狀態。
            window->update();
        });

        QObject::connect(window, &QQuickWindow::frameSwapped, &app,
                         [window, path, &app, done, &scenarioReady]() {
            if (*done || !scenarioReady.isReady()) {
                return;
            }
            *done = true;
            const QImage image = window->grabWindow();
            if (image.isNull() || !image.save(path)) {
                qCritical().noquote() << QStringLiteral("screenshot capture failed: %1").arg(path);
                app.exit(1);
                return;
            }
            app.exit(0);
        });

        window->setProperty("uiScenario", uiScenario);

        QTimer::singleShot(5000, &app, [window, &app, done, &scenarioReady, uiScenario]() {
            if (*done) {
                return;
            }
            *done = true;
            qCritical().noquote()
                << QStringLiteral("screenshot timed out waiting for uiScenarioReady: %1").arg(uiScenario)
                << QStringLiteral("(ready=%1)").arg(scenarioReady.isReady() ? "true" : "false");
            const auto *archived = window->findChild<QObject *>(QStringLiteral("archivedWorkspace"));
            const auto *temporary = window->findChild<QObject *>(QStringLiteral("temporaryWorkspace"));
            const auto *compare = window->findChild<QObject *>(QStringLiteral("compareWorkspace"));
            qCritical().noquote()
                << QStringLiteral("scenario diagnostics: active=%1 archived=%2/%3 temp=%4 compare=%5")
                       .arg(window->property("activeWorkspace").toString(),
                            archived == nullptr ? QStringLiteral("missing")
                                                : archived->property("workspaceState").toString(),
                            archived == nullptr ? QStringLiteral("missing")
                                                : archived->property("selectedDqeId").toString(),
                            temporary == nullptr ? QStringLiteral("missing")
                                                 : temporary->property("workspaceState").toString(),
                            compare == nullptr ? QStringLiteral("missing")
                                               : compare->property("chartReadyForCapture").toString());
            qCritical().noquote() << QStringLiteral("scenario handshake state: %1")
                                        .arg(window->property("scenarioHandshakeState").toString());
            qCritical().noquote() << QStringLiteral("scenario value on root: %1")
                                        .arg(window->property("uiScenario").toString());
            app.exit(1);
        });

        return app.exec();
    }

    if (parser.isSet(uiScenarioOption)) {
        window->setProperty("uiScenario", uiScenario);
    }

    // 啟動語言：背景 thread 查 daemon preferences.get_language（跟隨 terminal），拿到後以
    // QueuedConnection 設回 window.initialLanguage → DqeI18n.setLanguage。不阻塞啟動；
    // 只在正常互動路徑（此處已過 smoke/screenshot 的提早 return）且有 endpoint 時執行。
    if (hasEndpoint) {
        QPointer<QQuickWindow> winPtr(window);
        const std::string langEndpoint = endpoint.toStdString();
        std::thread([winPtr, langEndpoint]() {
            terminux::ipc::JsonRpcClientOptions langOpts;
            langOpts.socket_path = langEndpoint;
            langOpts.timeout_ms  = 1500;
            terminux::ipc::JsonRpcClient langClient(langOpts);
            QJsonObject langReq;
            langReq.insert(QStringLiteral("jsonrpc"), QStringLiteral("2.0"));
            langReq.insert(QStringLiteral("id"), 1);
            langReq.insert(QStringLiteral("method"), QStringLiteral("preferences.get_language"));
            langReq.insert(QStringLiteral("params"), QJsonObject{});
            const auto langResult = langClient.send_request(langReq);
            if (!langResult.transport_ok || langResult.response.contains(QStringLiteral("error"))) {
                return;
            }
            const QString lang = langResult.response.value(QStringLiteral("result")).toObject()
                                     .value(QStringLiteral("language")).toString();
            if (lang != QLatin1String("en") && lang != QLatin1String("zh-Hant")
                && lang != QLatin1String("zh-Hans")) {
                return;
            }
            // 回主 thread 設 property（QPointer 保命：視窗已關則略過）。
            QMetaObject::invokeMethod(qApp, [winPtr, lang]() {
                if (winPtr) {
                    winPtr->setProperty("initialLanguage", lang);
                }
            }, Qt::QueuedConnection);
        }).detach();
    }

    return app.exec();
}

#include "qml_main.moc"
