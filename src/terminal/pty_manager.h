// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
#pragma once

// PTY 生命週期管理（forkpty + QSocketNotifier 非同步讀取）。
// child 端注入 8 個 Terminux 環境變數。

#include <QByteArray>
#include <QObject>
#include <QString>

#include <sys/types.h>  // pid_t

class QSocketNotifier;

namespace terminux {

class PtyManager : public QObject {
    Q_OBJECT
public:
    explicit PtyManager(QObject* parent = nullptr);
    ~PtyManager() override;

    /// 啟動 shell。rows/cols 為初始 terminal 尺寸。shellProgram 為空時走
    /// $SHELL → /etc/passwd → /bin/bash 三段 fallback。回傳 fork 是否成功。
    bool start(int rows, int cols, const QString& shellProgram = {});

    /// 寫資料到 PTY（鍵盤輸入）。partial write 不處理（後續 round 補）。
    void writeData(const QByteArray& data);

    /// 改變 terminal 尺寸（TIOCSWINSZ；kernel 自動送 SIGWINCH 給 fg pgrp）。
    void resize(int rows, int cols);

    bool isRunning() const;
    pid_t childPid() const;

signals:
    /// PTY 有輸出可讀
    void dataReady(const QByteArray& data);
    /// 子程式結束（exitCode = WEXITSTATUS；signal 結束時為 -1）
    void finished(int exitCode);

private slots:
    void onReadReady();

private:
    void setupChildEnvironment();   // 在 child process 內呼叫（fork 後 / exec 前）
    void cleanup();                  // dtor 與重新 start 時用

    int               ptyFd_         = -1;
    pid_t             childPid_      = -1;
    QSocketNotifier*  readNotifier_  = nullptr;
    int               rows_          = 24;
    int               cols_          = 80;
    bool              finishedEmitted_ = false;

    // 由 start()（parent）預先計算，setupChildEnvironment（child）使用
    QByteArray traceRoot_;
    QByteArray traceSelf_;
    QByteArray bashEnv_;
    QByteArray origBashEnv_;   // 空 = 原本就沒有 BASH_ENV
};

}  // namespace terminux
