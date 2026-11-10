// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#include "terminal/work_dock.h"

#include "ipc/jsonrpc_client.h"
#include "ipc/agent_socket_path.h"

#include <QComboBox>
#include <QDir>
#include <QFileDialog>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QScrollArea>
#include <QSpinBox>
#include <QThread>
#include <QTimer>
#include <QUuid>
#include <QVBoxLayout>

namespace {

QPushButton* button(const QString& label, const char* objectName, QWidget* parent)
{
    auto* result = new QPushButton(label, parent);
    result->setObjectName(QString::fromLatin1(objectName));
    return result;
}

QJsonObject workParams(const QString& workId)
{
    return QJsonObject{{QStringLiteral("work_id"), workId}};
}

}  // namespace

WorkDock::WorkDock(QWidget* parent)
    : QDockWidget(QStringLiteral("工程工作"), parent)
{
    setObjectName(QStringLiteral("engineeringWorkDock"));
    auto* body = new QWidget(this);
    auto* vertical = new QVBoxLayout(body);
    auto* form = new QFormLayout();
    profilePath_ = new QLineEdit(body);
    profilePath_->setObjectName(QStringLiteral("workProfilePath"));
    auto* profileRow = new QWidget(body);
    auto* profileLayout = new QHBoxLayout(profileRow);
    profileLayout->setContentsMargins(0, 0, 0, 0);
    profileLayout->addWidget(profilePath_);
    auto* browseButton = button(QStringLiteral("瀏覽"), "workBrowseButton", profileRow);
    profileLayout->addWidget(browseButton);
    form->addRow(QStringLiteral("Profile 檔"), profileRow);
    profileId_ = new QLineEdit(body);
    profileId_->setObjectName(QStringLiteral("workProfileId"));
    form->addRow(QStringLiteral("Profile ID"), profileId_);
    packageId_ = new QLineEdit(QStringLiteral("orfs_run_intelligence"), body);
    packageId_->setObjectName(QStringLiteral("workPackageId"));
    form->addRow(QStringLiteral("Package"), packageId_);
    sourceRoot_ = new QLineEdit(body);
    sourceRoot_->setObjectName(QStringLiteral("workSourceRoot"));
    form->addRow(QStringLiteral("來源目錄"), sourceRoot_);
    objective_ = new QLineEdit(body);
    objective_->setObjectName(QStringLiteral("workObjective"));
    form->addRow(QStringLiteral("工程目標"), objective_);
    workIdEdit_ = new QLineEdit(body);
    workIdEdit_->setObjectName(QStringLiteral("workId"));
    form->addRow(QStringLiteral("Work ID"), workIdEdit_);
    mode_ = new QComboBox(body);
    mode_->setObjectName(QStringLiteral("workMode"));
    mode_->addItems({QStringLiteral("PLAN_ONLY"), QStringLiteral("PER_PROPOSAL"),
                     QStringLiteral("BOUNDED_EXPLORATION")});
    mode_->setCurrentIndex(2);
    form->addRow(QStringLiteral("授權模式"), mode_);
    attempts_ = new QSpinBox(body);
    attempts_->setObjectName(QStringLiteral("workAttempts"));
    attempts_->setRange(0, 100);
    attempts_->setValue(1);
    form->addRow(QStringLiteral("嘗試上限"), attempts_);
    providerDispatches_ = new QSpinBox(body);
    providerDispatches_->setObjectName(QStringLiteral("workProviderDispatches"));
    providerDispatches_->setRange(0, 1000);
    providerDispatches_->setValue(5);
    form->addRow(QStringLiteral("Provider 上限"), providerDispatches_);
    toolLaunches_ = new QSpinBox(body);
    toolLaunches_->setObjectName(QStringLiteral("workToolLaunches"));
    toolLaunches_->setRange(0, 100);
    toolLaunches_->setValue(2);
    form->addRow(QStringLiteral("Tool launch 上限"), toolLaunches_);
    candidateWrites_ = new QSpinBox(body);
    candidateWrites_->setObjectName(QStringLiteral("workCandidateWrites"));
    candidateWrites_->setRange(0, 100);
    candidateWrites_->setValue(1);
    form->addRow(QStringLiteral("候選寫入上限"), candidateWrites_);
    answer_ = new QLineEdit(body);
    answer_->setObjectName(QStringLiteral("workAnswer"));
    form->addRow(QStringLiteral("補充資料"), answer_);
    vertical->addLayout(form);

    auto* firstRow = new QHBoxLayout();
    selectButton_ = button(QStringLiteral("選 Profile"), "workSelectButton", body);
    createButton_ = button(QStringLiteral("建立"), "workCreateButton", body);
    previewButton_ = button(QStringLiteral("授權預覽"), "workPreviewButton", body);
    approveButton_ = button(QStringLiteral("核准"), "workApproveButton", body);
    for (auto* item : {selectButton_, createButton_, previewButton_, approveButton_})
        firstRow->addWidget(item);
    vertical->addLayout(firstRow);
    auto* secondRow = new QHBoxLayout();
    getButton_ = button(QStringLiteral("查詢"), "workGetButton", body);
    takeoverButton_ = button(QStringLiteral("重新接管"), "workTakeoverButton", body);
    pauseButton_ = button(QStringLiteral("暫停"), "workPauseButton", body);
    resumeButton_ = button(QStringLiteral("恢復"), "workResumeButton", body);
    cancelButton_ = button(QStringLiteral("取消"), "workCancelButton", body);
    answerButton_ = button(QStringLiteral("送出補充"), "workAnswerButton", body);
    for (auto* item : {getButton_, takeoverButton_, pauseButton_, resumeButton_, cancelButton_, answerButton_})
        secondRow->addWidget(item);
    vertical->addLayout(secondRow);
    statusSummary_ = new QLabel(QStringLiteral("尚未查詢工程工作"), body);
    statusSummary_->setObjectName(QStringLiteral("workStatusSummary"));
    statusSummary_->setWordWrap(true);
    vertical->addWidget(statusSummary_);
    output_ = new QPlainTextEdit(body);
    output_->setObjectName(QStringLiteral("workResponse"));
    output_->setReadOnly(true);
    output_->setPlaceholderText(QStringLiteral("正式 Work RPC 回覆會顯示於此；查詢不會推進工作。"));
    vertical->addWidget(output_, 1);
    setWidget(body);

    const auto lineageEndpoint = terminux::resolveLineageSocketPath(false);
    lineageSocketPath_ = lineageEndpoint.path;
    if (!lineageEndpoint.ok) statusSummary_->setText(lineageEndpoint.message);
    refreshTimer_ = new QTimer(this);
    refreshTimer_->setInterval(5000);
    connect(refreshTimer_, &QTimer::timeout, this, &WorkDock::getWork);
    connect(browseButton, &QPushButton::clicked, this, [this]() {
        const QString path = QFileDialog::getOpenFileName(this, QStringLiteral("選擇 Profile"));
        if (!path.isEmpty()) profilePath_->setText(path);
    });
    connect(selectButton_, &QPushButton::clicked, this, &WorkDock::selectProfile);
    connect(createButton_, &QPushButton::clicked, this, &WorkDock::createWork);
    connect(previewButton_, &QPushButton::clicked, this, &WorkDock::previewAuthorization);
    connect(approveButton_, &QPushButton::clicked, this, &WorkDock::approveWork);
    connect(getButton_, &QPushButton::clicked, this, &WorkDock::getWork);
    connect(takeoverButton_, &QPushButton::clicked, this, [this]() { operateWork(QStringLiteral("takeover")); });
    connect(pauseButton_, &QPushButton::clicked, this, [this]() { operateWork(QStringLiteral("pause")); });
    connect(resumeButton_, &QPushButton::clicked, this, [this]() { operateWork(QStringLiteral("resume")); });
    connect(cancelButton_, &QPushButton::clicked, this, [this]() { operateWork(QStringLiteral("cancel")); });
    connect(answerButton_, &QPushButton::clicked, this, &WorkDock::answerWork);
    connect(workIdEdit_, &QLineEdit::textChanged, this, [this]() {
        lastPreview_ = {};
        workId_ = workIdEdit_->text().trimmed();
        updateButtons();
    });
    connect(mode_, &QComboBox::currentTextChanged, this, [this](const QString& mode) {
        if (mode == QLatin1String("PLAN_ONLY")) {
            attempts_->setValue(0);
            toolLaunches_->setValue(0);
            candidateWrites_->setValue(0);
        } else if (attempts_->value() == 0) {
            attempts_->setValue(1);
            toolLaunches_->setValue(2);
            candidateWrites_->setValue(1);
        }
        lastPreview_ = {};
        updateButtons();
    });
    for (auto* limit : {attempts_, providerDispatches_, toolLaunches_, candidateWrites_}) {
        connect(limit, qOverload<int>(&QSpinBox::valueChanged), this, [this]() {
            lastPreview_ = {};
            updateButtons();
        });
    }
    updateButtons();
}

WorkDock::~WorkDock()
{
    if (activeThread_) activeThread_->wait();
}

void WorkDock::setSessionId(const QString& sessionId)
{
    sessionId_ = sessionId;
    if (sessionId_.isEmpty()) {
        refreshTimer_->stop();
        lastPreview_ = {};
    }
    updateButtons();
}

void WorkDock::setLineageSocketPath(const QString& socketPath)
{
    lineageSocketPath_ = socketPath;
}

void WorkDock::updateButtons()
{
    const bool ready = !sessionId_.isEmpty() && !busy_;
    const bool hasWork = ready && !workId_.isEmpty();
    selectButton_->setEnabled(ready);
    createButton_->setEnabled(ready);
    previewButton_->setEnabled(hasWork);
    approveButton_->setEnabled(hasWork && !lastPreview_.isEmpty());
    for (auto* item : {getButton_, takeoverButton_, pauseButton_, resumeButton_, cancelButton_, answerButton_})
        item->setEnabled(hasWork);
}

void WorkDock::dispatch(const QString& method, const QJsonObject& params)
{
    if (sessionId_.isEmpty() || busy_) return;
    busy_ = true;
    updateButtons();
    output_->setPlainText(QStringLiteral("等候 daemon 回覆：%1").arg(method));
    const QJsonObject request{
        {QStringLiteral("jsonrpc"), QStringLiteral("2.0")},
        {QStringLiteral("id"), QUuid::createUuid().toString(QUuid::WithoutBraces)},
        {QStringLiteral("method"), method},
        {QStringLiteral("params"), params},
        {QStringLiteral("terminux_context"), QJsonObject{{QStringLiteral("session_id"), sessionId_}}},
    };
    const QString socketPath = lineageSocketPath_;
    auto* thread = QThread::create([this, socketPath, request, method]() {
        terminux::ipc::JsonRpcClient client({socketPath.toStdString(), 15000});
        const auto result = client.send_request(request);
        const QString error = QString::fromStdString(result.error_message);
        QMetaObject::invokeMethod(this, [this, method, response = result.response, error]() {
            finishRequest(method, response, error);
        }, Qt::QueuedConnection);
    });
    activeThread_ = thread;
    connect(thread, &QThread::finished, this, [this, thread]() {
        if (activeThread_ == thread) activeThread_.clear();
        thread->deleteLater();
    });
    thread->start();
}

void WorkDock::finishRequest(const QString& method, const QJsonObject& response,
                             const QString& transportError)
{
    busy_ = false;
    if (!transportError.isEmpty()) {
        output_->setPlainText(QStringLiteral("傳輸失敗：%1").arg(transportError));
        statusSummary_->setText(QStringLiteral("傳輸失敗：%1").arg(transportError));
        updateButtons();
        return;
    }
    output_->setPlainText(QString::fromUtf8(QJsonDocument(response).toJson(QJsonDocument::Indented)));
    const QJsonObject result = response.value(QStringLiteral("result")).toObject();
    if (response.contains(QStringLiteral("error"))
        || result.value(QStringLiteral("status")).toString() == QLatin1String("error")) {
        const QString code = result.value(QStringLiteral("error_code")).toString();
        statusSummary_->setText(QStringLiteral("操作未完成：%1").arg(
            code.isEmpty() ? QStringLiteral("JSON-RPC 錯誤") : code));
        updateButtons();
        return;
    }
    const QJsonObject work = result.value(QStringLiteral("work")).toObject();
    if (!work.isEmpty()) {
        const QString workStatus = work.value(QStringLiteral("status")).toString();
        const QString reason = work.value(QStringLiteral("stop_reason")).toString();
        statusSummary_->setText(QStringLiteral("Work %1｜%2%3").arg(
            work.value(QStringLiteral("work_id")).toString(), workStatus,
            reason.isEmpty() ? QString() : QStringLiteral("｜%1").arg(reason)));
    } else if (method == QLatin1String("agent.eap3.work.authorization_preview")) {
        statusSummary_->setText(QStringLiteral("授權預覽已讀回；請核對完整內容後核准"));
    }
    if (method == QLatin1String("agent.flow_task.select_profile")) {
        const QString selected = result.value(QStringLiteral("profile")).toObject()
                                     .value(QStringLiteral("profile_id")).toString();
        if (!selected.isEmpty()) profileId_->setText(selected);
    } else if (method == QLatin1String("agent.eap3.work.create")) {
        const QString id = result.value(QStringLiteral("work")).toObject()
                               .value(QStringLiteral("work_id")).toString();
        if (!id.isEmpty()) {
            workIdEdit_->setText(id);
            workId_ = id;
        }
    } else if (method == QLatin1String("agent.eap3.work.authorization_preview")) {
        lastPreview_ = result;
    } else if (method == QLatin1String("agent.eap3.work.approve")) {
        lastPreview_ = {};
        refreshTimer_->start();
    } else if (method == QLatin1String("agent.eap3.work.get")) {
        const QString status = result.value(QStringLiteral("work")).toObject()
                                .value(QStringLiteral("status")).toString();
        if (status == QLatin1String("COMPLETED") || status == QLatin1String("STOPPED")
            || status == QLatin1String("CANCELLED") || status == QLatin1String("UNCONFIRMED"))
            refreshTimer_->stop();
    }
    updateButtons();
}

void WorkDock::selectProfile()
{
    const QString path = profilePath_->text().trimmed();
    if (path.isEmpty()) return;
    lastPreview_ = {};
    dispatch(QStringLiteral("agent.flow_task.select_profile"),
             QJsonObject{{QStringLiteral("profile_path"), path}});
}

void WorkDock::createWork()
{
    const QString profile = profileId_->text().trimmed();
    const QString package = packageId_->text().trimmed();
    const QString objective = objective_->text().trimmed();
    if (profile.isEmpty() || package.isEmpty() || objective.isEmpty()) {
        output_->setPlainText(QStringLiteral("請填入 Profile ID、Package 與工程目標。"));
        return;
    }
    QJsonObject params{{QStringLiteral("profile_id"), profile},
                       {QStringLiteral("package_id"), package},
                       {QStringLiteral("objective"), objective},
                       {QStringLiteral("criteria"), QJsonArray{}},
                       {QStringLiteral("constraints"), QJsonArray{}}};
    const QString source = sourceRoot_->text().trimmed();
    if (!source.isEmpty()) params.insert(QStringLiteral("source_root"), source);
    lastPreview_ = {};
    dispatch(QStringLiteral("agent.eap3.work.create"), params);
}

void WorkDock::previewAuthorization()
{
    if (workId_.isEmpty()) return;
    lastPreview_ = {};
    const QJsonObject limits{
        {QStringLiteral("engineering_attempts"), attempts_->value()},
        {QStringLiteral("provider_dispatches"), providerDispatches_->value()},
        {QStringLiteral("tool_launches"), toolLaunches_->value()},
        {QStringLiteral("candidate_materializations"), candidateWrites_->value()},
    };
    dispatch(QStringLiteral("agent.eap3.work.authorization_preview"),
             QJsonObject{{QStringLiteral("work_id"), workId_},
                         {QStringLiteral("mode"), mode_->currentText()},
                         {QStringLiteral("limits"), limits}});
}

void WorkDock::approveWork()
{
    if (workId_.isEmpty() || lastPreview_.isEmpty()) return;
    // 完整 JSON 已在 Work dock 的可捲動輸出顯示；確認視窗只顯示關鍵摘要，避免超出螢幕。
    const QJsonObject authorization = lastPreview_.value(QStringLiteral("authorization")).toObject();
    const QString digest = authorization.value(QStringLiteral("authorization_digest")).toString();
    const QJsonObject limits = authorization.value(QStringLiteral("limits")).toObject();
    const QString summary = QStringLiteral(
        "請先核對 Work dock 中的完整授權預覽。\n\n"
        "Digest：%1\n模式：%2\n"
        "工程嘗試：%3；Provider：%4；工具：%5；候選寫入：%6\n\n"
        "確定核准並啟動這件 work？")
        .arg(digest,
             authorization.value(QStringLiteral("mode")).toString(),
             QString::number(limits.value(QStringLiteral("engineering_attempts")).toInt()),
             QString::number(limits.value(QStringLiteral("provider_dispatches")).toInt()),
             QString::number(limits.value(QStringLiteral("tool_launches")).toInt()),
             QString::number(limits.value(QStringLiteral("candidate_materializations")).toInt()));
    QMessageBox confirmation(QMessageBox::Question, QStringLiteral("核准工程工作"), summary,
                             QMessageBox::Yes | QMessageBox::No, this);
    confirmation.setDefaultButton(QMessageBox::No);
    confirmation.setStyleSheet(QStringLiteral("QMessageBox { background-color: #f5f5f5; color: #111111; } "
                                              "QPushButton { background-color: #ffffff; color: #111111; }"));
    if (confirmation.exec() != QMessageBox::Yes) return;
    dispatch(QStringLiteral("agent.eap3.work.approve"), workParams(workId_));
}

void WorkDock::getWork()
{
    if (!workId_.isEmpty()) dispatch(QStringLiteral("agent.eap3.work.get"), workParams(workId_));
}

void WorkDock::operateWork(const QString& operation)
{
    if (workId_.isEmpty()) return;
    if (operation == QLatin1String("cancel")
        && QMessageBox::question(this, QStringLiteral("取消工程工作"),
                                 QStringLiteral("確定取消這件 work？"),
                                 QMessageBox::Yes | QMessageBox::No,
                                 QMessageBox::No) != QMessageBox::Yes)
        return;
    dispatch(QStringLiteral("agent.eap3.work.") + operation, workParams(workId_));
}

void WorkDock::answerWork()
{
    const QString message = answer_->text().trimmed();
    if (workId_.isEmpty() || message.isEmpty()) return;
    dispatch(QStringLiteral("agent.eap3.work.answer"),
             QJsonObject{{QStringLiteral("work_id"), workId_},
                         {QStringLiteral("message"), message}});
}
