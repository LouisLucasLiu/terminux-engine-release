// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include <QApplication>
#include <QAction>
#include <QComboBox>
#include <QDebug>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLineEdit>
#include <QLibraryInfo>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSpinBox>
#include <QTimer>
#include <cstdio>
#include <cstring>
#include <memory>
#include "terminal/main_window.h"
#include "terminal/work_dock.h"

int main(int argc, char* argv[])
{
    bool selfCheck = false;
    for (int i = 1; i < argc; ++i) {
        selfCheck = selfCheck || std::strcmp(argv[i], "--self-check") == 0;
    }
    if (selfCheck) {
        qputenv("QT_QPA_PLATFORM", "offscreen");
    }
    for (int i = 1; i < argc; ++i) {
        if (std::strcmp(argv[i], "--version") == 0) {
            std::printf("Terminux %s\n"
                        "Copyright 2026 Liu, Hsien-Ming (Louis Liu)\n"
                        "License: PolyForm Shield 1.0.0 <https://polyformproject.org/licenses/shield/1.0.0/>\n",
                        TERMINUX_VERSION);
            return 0;
        }
    }

    QApplication app(argc, argv);
    // 在建立視窗、PTY 與 daemon client 之前結束安裝檢查。
    if (selfCheck) {
        qInfo().noquote() << QStringLiteral("GUI 自我檢查通過；Qt %1；plugins=%2")
            .arg(QString::fromLatin1(qVersion()), QLibraryInfo::path(QLibraryInfo::PluginsPath));
        return 0;
    }
    app.setApplicationName(QStringLiteral("Terminux"));
    app.setOrganizationName(QStringLiteral("Terminux"));
    app.setApplicationVersion(QStringLiteral(TERMINUX_VERSION));

    MainWindow w;
    w.show();

    // 離線 release qualification：實際 executable 點擊既有 Work dock 控制，
    // 每一步都等待正式 daemon RPC 回覆；不直接呼叫內部 service。
    const int qualificationIndex = app.arguments().indexOf(QStringLiteral("--eap5-work-qualification"));
    if (qualificationIndex >= 0) {
        struct QualificationState {
            QJsonObject scenario;
            QJsonArray results;
            int index = 0;
            bool waiting = false;
            bool inClick = false;
            bool finished = false;
        };
        auto state = std::make_shared<QualificationState>();
        auto* dock = w.findChild<WorkDock*>(QStringLiteral("engineeringWorkDock"));
        auto* panelAction = w.findChild<QAction*>(QStringLiteral("workPanelAction"));
        const bool defaultHidden = dock && dock->isHidden() && !dock->isVisible();
        if (defaultHidden && panelAction) panelAction->trigger();
        const bool openedFromAction = dock && dock->isVisible();
        const QString path = qualificationIndex + 1 < app.arguments().size()
            ? app.arguments().at(qualificationIndex + 1) : QString();
        QFile scenarioFile(path);
        if (scenarioFile.open(QIODevice::ReadOnly))
            state->scenario = QJsonDocument::fromJson(scenarioFile.readAll()).object();
        auto finish = [&app, &w, state, defaultHidden, openedFromAction](const QString& reason, int code) {
            if (state->finished) return;
            state->finished = true;
            const QString screenshotPath = state->scenario.value(
                QStringLiteral("screenshot_path")).toString();
            const bool screenshotSaved = !screenshotPath.isEmpty()
                && w.grab().save(screenshotPath, "PNG");
            const QJsonObject result{
                {QStringLiteral("status"), code == 0 ? QStringLiteral("PASSED") : QStringLiteral("FAILED")},
                {QStringLiteral("reason"), reason},
                {QStringLiteral("actions"), state->results},
                {QStringLiteral("screenshot_saved"), screenshotSaved},
                {QStringLiteral("work_dock_default_hidden"), defaultHidden},
                {QStringLiteral("work_dock_opened_from_action"), openedFromAction},
            };
            fputs(QJsonDocument(result).toJson(QJsonDocument::Compact).constData(), stdout);
            fputc('\n', stdout);
            fflush(stdout);
            app.exit(code);
        };
        const QJsonArray actions = state->scenario.value(QStringLiteral("actions")).toArray();
        if (!defaultHidden || !openedFromAction) {
            QTimer::singleShot(0, &app, [finish]() { finish(QStringLiteral("work_panel_visibility_failed"), 1); });
        } else if (actions.isEmpty() || actions.size() > 20) {
            QTimer::singleShot(0, &app, [finish]() { finish(QStringLiteral("invalid_actions"), 2); });
        } else {
            auto* driver = new QTimer(&app);
            driver->setInterval(40);
            QObject::connect(driver, &QTimer::timeout, &app, [&w, state, actions, finish]() {
                if (state->finished || state->inClick) return;
                auto* dock = w.findChild<WorkDock*>(QStringLiteral("engineeringWorkDock"));
                auto* output = w.findChild<QPlainTextEdit*>(QStringLiteral("workResponse"));
                auto* create = w.findChild<QPushButton*>(QStringLiteral("workCreateButton"));
                if (!dock || !output || !create || !create->isEnabled()) return;
                if (state->waiting) {
                    const QJsonObject response = QJsonDocument::fromJson(
                        output->toPlainText().toUtf8()).object();
                    if (!response.contains(QStringLiteral("result"))
                        && !response.contains(QStringLiteral("error"))) return;
                    const QJsonObject result = response.value(QStringLiteral("result")).toObject();
                    const QString action = actions.at(state->index).toString();
                    const QString status = result.value(QStringLiteral("status")).toString();
                    state->results.append(QJsonObject{
                        {QStringLiteral("action"), action},
                        {QStringLiteral("status"), status},
                        {QStringLiteral("error_code"), result.value(QStringLiteral("error_code"))},
                        {QStringLiteral("work_id"), result.value(QStringLiteral("work"))
                            .toObject().value(QStringLiteral("work_id"))},
                        {QStringLiteral("work_status"), result.value(QStringLiteral("work"))
                            .toObject().value(QStringLiteral("status"))},
                    });
                    if (response.contains(QStringLiteral("error")) || status != QLatin1String("ok")) {
                        finish(QStringLiteral("rpc_failed:" ) + action, 1);
                        return;
                    }
                    state->waiting = false;
                    ++state->index;
                    if (state->index == actions.size()) {
                        finish(QStringLiteral("all_actions_completed"), 0);
                        return;
                    }
                }
                const QString action = actions.at(state->index).toString();
                const QHash<QString, QString> buttons{
                    {QStringLiteral("select"), QStringLiteral("workSelectButton")},
                    {QStringLiteral("create"), QStringLiteral("workCreateButton")},
                    {QStringLiteral("preview"), QStringLiteral("workPreviewButton")},
                    {QStringLiteral("approve"), QStringLiteral("workApproveButton")},
                    {QStringLiteral("get"), QStringLiteral("workGetButton")},
                    {QStringLiteral("pause"), QStringLiteral("workPauseButton")},
                    {QStringLiteral("resume"), QStringLiteral("workResumeButton")},
                    {QStringLiteral("cancel"), QStringLiteral("workCancelButton")},
                    {QStringLiteral("takeover"), QStringLiteral("workTakeoverButton")},
                    {QStringLiteral("answer"), QStringLiteral("workAnswerButton")},
                };
                auto* button = w.findChild<QPushButton*>(buttons.value(action).toLatin1().constData());
                if (!buttons.contains(action) || !button || !button->isEnabled()) {
                    finish(QStringLiteral("button_unavailable:") + action, 1);
                    return;
                }
                if (action == QLatin1String("select")) {
                    w.findChild<QLineEdit*>(QStringLiteral("workProfilePath"))->setText(
                        state->scenario.value(QStringLiteral("profile_path")).toString());
                } else if (action == QLatin1String("create")) {
                    w.findChild<QLineEdit*>(QStringLiteral("workObjective"))->setText(
                        state->scenario.value(QStringLiteral("objective")).toString());
                    w.findChild<QLineEdit*>(QStringLiteral("workPackageId"))->setText(
                        state->scenario.value(QStringLiteral("package_id")).toString());
                    w.findChild<QLineEdit*>(QStringLiteral("workSourceRoot"))->setText(
                        state->scenario.value(QStringLiteral("source_root")).toString());
                } else if (action == QLatin1String("preview")) {
                    auto* mode = w.findChild<QComboBox*>(QStringLiteral("workMode"));
                    mode->setCurrentText(state->scenario.value(QStringLiteral("mode"))
                                             .toString(QStringLiteral("BOUNDED_EXPLORATION")));
                    if (state->scenario.contains(QStringLiteral("provider_dispatches"))) {
                        w.findChild<QSpinBox*>(QStringLiteral("workProviderDispatches"))->setValue(
                            state->scenario.value(QStringLiteral("provider_dispatches")).toInt());
                    }
                } else if (action == QLatin1String("answer")) {
                    w.findChild<QLineEdit*>(QStringLiteral("workAnswer"))->setText(
                        state->scenario.value(QStringLiteral("answer")).toString());
                }
                if (action == QLatin1String("approve") || action == QLatin1String("cancel")) {
                    QTimer::singleShot(0, []() {
                        for (QWidget* widget : QApplication::topLevelWidgets()) {
                            if (auto* dialog = qobject_cast<QMessageBox*>(widget)) {
                                dialog->button(QMessageBox::Yes)->click();
                                return;
                            }
                        }
                    });
                }
                state->inClick = true;
                button->click();
                state->inClick = false;
                state->waiting = true;
            });
            driver->start();
            QTimer::singleShot(60000, &app, [finish]() {
                finish(QStringLiteral("qualification_timeout"), 1);
            });
        }
    }

    // 發行產物的離線啟動檢查：走真正 executable／MainWindow／session，
    // 定時正常退出，避免測試以 SIGKILL 留下 PTY child。
    if (app.arguments().contains(QStringLiteral("--eap5-smoke"))) {
        auto* dock = w.findChild<WorkDock*>(QStringLiteral("engineeringWorkDock"));
        auto* panelAction = w.findChild<QAction*>(QStringLiteral("workPanelAction"));
        const bool defaultHidden = dock && dock->isHidden() && !dock->isVisible();
        if (defaultHidden && panelAction) panelAction->trigger();
        const int readbackIndex = app.arguments().indexOf(QStringLiteral("--eap5-smoke-work-id"));
        const QString readbackId = readbackIndex >= 0 && readbackIndex + 1 < app.arguments().size()
            ? app.arguments().at(readbackIndex + 1) : QString();
        if (!readbackId.isEmpty()) {
            QTimer::singleShot(1000, &app, [&w, readbackId]() {
                auto* field = w.findChild<QLineEdit*>(QStringLiteral("workId"));
                auto* get = w.findChild<QPushButton*>(QStringLiteral("workGetButton"));
                if (field && get) {
                    field->setText(readbackId);
                    if (get->isEnabled()) get->click();
                }
            });
        }
        QTimer::singleShot(3000, &app, [&app, &w, readbackId, defaultHidden]() {
            auto* dock = w.findChild<WorkDock*>(QStringLiteral("engineeringWorkDock"));
            auto* create = w.findChild<QPushButton*>(QStringLiteral("workCreateButton"));
            QJsonObject result{
                {QStringLiteral("work_dock_default_hidden"), defaultHidden},
                {QStringLiteral("work_dock_visible"), dock && dock->isVisible()},
                {QStringLiteral("session_ready"), create && create->isEnabled()},
            };
            if (!readbackId.isEmpty()) {
                auto* output = w.findChild<QPlainTextEdit*>(QStringLiteral("workResponse"));
                const QJsonObject response = output
                    ? QJsonDocument::fromJson(output->toPlainText().toUtf8()).object()
                    : QJsonObject{};
                const QJsonObject work = response.value(QStringLiteral("result"))
                    .toObject().value(QStringLiteral("work")).toObject();
                result.insert(QStringLiteral("work_readback_id"),
                              work.value(QStringLiteral("work_id")));
                result.insert(QStringLiteral("work_readback_status"),
                              work.value(QStringLiteral("status")));
            }
            fputs(QJsonDocument(result).toJson(QJsonDocument::Compact).constData(), stdout);
            fputc('\n', stdout);
            app.quit();
        });
    }

    return app.exec();
}
