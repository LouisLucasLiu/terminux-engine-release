// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 Liu, Hsien-Ming (Louis Liu)
// workspace 唯一 repository provider surface；不 import backend/transport 模組。

import QtQml

QtObject {
    id: facade
    objectName: "dqeRepositoryFacade"

    // 呼叫端注入 production controller；mock workspace 仍維持既有直接 wiring。
    required property var provider

    readonly property var sources: provider.sources
    readonly property string repositoryState: provider.repositoryState
    readonly property string stateDetail: provider.stateDetail
    readonly property string selectedSourceId: provider.selectedSourceId
    readonly property string selectedItemId: provider.selectedItemId
    readonly property string selectedAttachmentId: provider.selectedAttachmentId
    readonly property string previewState: provider.previewState
    readonly property string previewKind: provider.previewKind
    readonly property string previewText: provider.previewText
    readonly property url previewImageUrl: provider.previewImageUrl
    readonly property string previewAttachmentId: provider.previewAttachmentId

    // production adapter surface。只讀取 controller 的 safe DTO，沒有 backend field。
    readonly property string dataState: repositoryState

    function refreshArchived() {
        return provider.requestSources()
    }

    function listArchivedSources() {
        // 將 C++ QVariantList 正規化為 JS Array，維持 navigator 的既有 Array contract。
        const mapped = []
        for (let index = 0; index < sources.length; ++index) {
            const source = sources[index]
            mapped.push({
                sourceId: source.sourceId,
                sourceKind: source.sourceKind,
                displayName: source.displayName,
                jobKey: source.jobOrVersion,
                jobOrVersion: source.jobOrVersion,
                block: source.block,
                flow: source.flow,
                dateTime: source.dateTime,
                availability: source.availability,
                lifecycleState: source.lifecycleState
            })
        }
        return mapped
    }

    function selectGroup(sourceId) {
        const requestedId = String(sourceId)
        for (let index = 0; index < sources.length; ++index) {
            if (String(sources[index].sourceId) === requestedId) {
                provider.setSelectedSource(requestedId)
                // setter 是 C++ void slot，QML 不應以同步 property binding 是否已重算作為成功條件。
                return true
            }
        }
        return false
    }

    function _displayStatus(status) {
        if (status === "pass") return "Pass"
        if (status === "warn") return "Warn"
        if (status === "fail") return "Fail"
        // not_applicable 沒有第六種 UI status，顯示 Unknown；statusRaw 仍保留。
        return "Unknown"
    }

    function _itemsForSelectedSource() {
        const sourceId = String(provider.selectedSourceId)
        if (sourceId.length === 0) return []
        const raw = provider.itemsForSource(sourceId)
        const mapped = []
        for (let index = 0; index < raw.length; ++index) {
            const item = raw[index]
            const attachments = provider.attachmentsForItem(item.itemId)
            const metadata = []
            for (let attachmentIndex = 0; attachmentIndex < attachments.length; ++attachmentIndex) {
                const attachment = attachments[attachmentIndex]
                metadata.push({
                    attachmentId: attachment.attachmentId,
                    displayName: attachment.displayName,
                    role: attachment.role,
                    state: attachment.state,
                    mimeHint: attachment.mimeHint,
                    sizeBytes: attachment.sizeBytes,
                    // metadata list 不暗示已讀取 payload；只有 explicit preview state 才可變更。
                    previewAvailability: "metadata_only",
                    integrityState: attachment.integrityState,
                    previewKind: "metadata_only",
                    mockPayload: null
                })
            }
            mapped.push({
                itemId: item.itemId,
                dqeId: item.dqeId,
                title: item.title,
                value: item.rawValue,
                rawValue: item.rawValue,
                unit: item.unit,
                status: _displayStatus(item.status),
                statusRaw: item.statusRaw,
                description: item.description,
                reference: item.reference,
                dateTime: item.dateTime,
                block: item.block,
                flow: item.flow,
                attachments: metadata
            })
        }
        return mapped
    }

    function getSummaryCounts() {
        const items = _itemsForSelectedSource()
        const counts = { total: items.length, pass: 0, warn: 0, fail: 0, unknown: 0 }
        for (let index = 0; index < items.length; ++index) {
            switch (items[index].status) {
            case "Pass": counts.pass++; break
            case "Warn": counts.warn++; break
            case "Fail": counts.fail++; break
            default: counts.unknown++; break
            }
        }
        return counts
    }

    function getItemList() {
        return _itemsForSelectedSource()
    }

    function selectItem(dqeId) {
        const items = _itemsForSelectedSource()
        for (let index = 0; index < items.length; ++index) {
            if (items[index].dqeId === dqeId) {
                provider.setSelectedItem(String(provider.selectedSourceId), items[index].itemId)
                return true
            }
        }
        return false
    }

    function getSelectedItem() {
        const items = _itemsForSelectedSource()
        for (let index = 0; index < items.length; ++index) {
            if (items[index].itemId === String(provider.selectedItemId)) return items[index]
        }
        return null
    }

    function getSelectedAttachments() {
        const item = getSelectedItem()
        return item === null ? [] : item.attachments
    }

    function selectAttachment(attachmentId) {
        provider.setSelectedAttachment(attachmentId)
        return true
    }

    function listTemporaryJobs() {
        // C++ QVariantList 正規化為 JS Array，符合 navigator 的既有 contract。
        const mapped = []
        for (let index = 0; index < sources.length; ++index) {
            const source = sources[index]
            if (source.sourceKind !== "temporary") continue
            const sourceItems = provider.itemsForSource(String(source.sourceId))
            const counts = { pass: 0, warn: 0, fail: 0, unknown: 0 }
            for (let itemIndex = 0; itemIndex < sourceItems.length; ++itemIndex) {
                switch (String(sourceItems[itemIndex].status)) {
                case "pass": counts.pass++; break
                case "warn": counts.warn++; break
                case "fail": counts.fail++; break
                default: counts.unknown++; break
                }
            }
            mapped.push({
                sourceId: source.sourceId,
                jobKey: source.jobOrVersion,
                block: source.block,
                flow: source.flow,
                executionTime: source.dateTime,
                retentionDays: source.retentionDays,
                expiresAt: source.expiresAt,
                expired: source.isExpired === true,
                lifecycleState: source.lifecycleState,
                canCheckin: source.canCheckin === true,
                pass: counts.pass,
                warn: counts.warn,
                fail: counts.fail,
                unknown: counts.unknown
            })
        }
        return mapped
    }

    function selectTemporaryJob(sourceId) {
        return selectGroup(sourceId)
    }

    function loadTemporaryJob(sourceId) {
        return provider.requestItems(String(sourceId))
    }

    function refreshSources() {
        return provider.requestSources()
    }

    function loadItems(sourceId) {
        return provider.requestItems(sourceId)
    }

    // 只由 explicit activation 觸發 opaque attachment 的 memory-only preview。
    function requestPreview(attachmentId) {
        const requestedId = String(attachmentId)
        if (String(provider.selectedAttachmentId) !== requestedId) {
            provider.setSelectedAttachment(requestedId)
        }
        return provider.requestPreviewState(requestedId)
    }

    function clearPreview() {
        provider.clearPreview()
    }

    function itemsForSource(sourceId) {
        return provider.itemsForSource(sourceId)
    }

    function attachmentsForItem(itemId) {
        return provider.attachmentsForItem(itemId)
    }

    function selectSource(sourceId) {
        provider.setSelectedSource(sourceId)
    }

    function selectOpaqueItem(sourceId, itemId) {
        provider.setSelectedItem(sourceId, itemId)
    }
}
