// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

#include <QDockWidget>
#include <QJsonObject>
#include <QPointer>
#include <QString>

class QComboBox;
class QLineEdit;
class QLabel;
class QPlainTextEdit;
class QPushButton;
class QSpinBox;
class QThread;
class QTimer;

/**
 * 使用既有 lineage Work RPC 的 Qt 入口；所有授權與範圍判定仍在 daemon。
 */
class WorkDock final : public QDockWidget
{
    Q_OBJECT

public:
    explicit WorkDock(QWidget* parent = nullptr);
    ~WorkDock() override;

    void setSessionId(const QString& sessionId);
    void setLineageSocketPath(const QString& socketPath);

private:
    void dispatch(const QString& method, const QJsonObject& params);
    void finishRequest(const QString& method, const QJsonObject& response,
                       const QString& transportError);
    void updateButtons();
    void selectProfile();
    void createWork();
    void previewAuthorization();
    void approveWork();
    void getWork();
    void operateWork(const QString& operation);
    void answerWork();

    QString sessionId_;
    QString lineageSocketPath_;
    QString workId_;
    QJsonObject lastPreview_;
    QPointer<QThread> activeThread_;
    bool busy_ = false;

    QLineEdit* profilePath_ = nullptr;
    QLineEdit* profileId_ = nullptr;
    QLineEdit* packageId_ = nullptr;
    QLineEdit* sourceRoot_ = nullptr;
    QLineEdit* objective_ = nullptr;
    QLineEdit* workIdEdit_ = nullptr;
    QLineEdit* answer_ = nullptr;
    QComboBox* mode_ = nullptr;
    QSpinBox* attempts_ = nullptr;
    QSpinBox* providerDispatches_ = nullptr;
    QSpinBox* toolLaunches_ = nullptr;
    QSpinBox* candidateWrites_ = nullptr;
    QPushButton* selectButton_ = nullptr;
    QPushButton* createButton_ = nullptr;
    QPushButton* previewButton_ = nullptr;
    QPushButton* approveButton_ = nullptr;
    QPushButton* getButton_ = nullptr;
    QPushButton* takeoverButton_ = nullptr;
    QPushButton* pauseButton_ = nullptr;
    QPushButton* resumeButton_ = nullptr;
    QPushButton* cancelButton_ = nullptr;
    QPushButton* answerButton_ = nullptr;
    QPlainTextEdit* output_ = nullptr;
    QLabel* statusSummary_ = nullptr;
    QTimer* refreshTimer_ = nullptr;
};
