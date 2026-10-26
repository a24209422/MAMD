---
name: tdd-crap
description: 在 Python 專案新增功能、修正錯誤或依 TDD 開發時使用。執行 Red、Green、Refactor，並以 pytest、coverage.py、Radon 檢查每個函式的覆蓋率、循環複雜度與 CRAP。
---

# TDD + CRAP

## 開始前

- 讀取適用的 AGENTS.md 或 CLAUDE.md、專案設定與既有測試。
- 優先沿用現有工具、命名與程式結構。
- 定義可觀察的驗收條件，列出正常、邊界與失敗案例。
- 確認來源與測試目錄，不任意擴大檢查範圍。
- 確認 requirements.txt 的套件已安裝在專案的 Python 環境；
  quality.py 會用執行它的 python 跑 pytest，缺套件時直接回報錯誤。

## 開發循環

1. 選一個行為，先新增或修改測試。
2. 實際執行測試，確認因預期行為尚未成立而失敗。
3. 修正測試環境問題；匯入錯誤或測試收集失敗不算有效 Red。
4. 寫足以讓測試通過的實作。
5. 保持測試通過，整理命名、重複與職責。
6. 執行相關回歸測試及品質檢查，再處理下一個行為。

純重構先確認既有測試通過。純文件、註解或格式修改使用適當檢查，
不為湊流程新增無行為價值的測試。

## 品質檢查

在專案根目錄，用專案環境的 python 執行：

    python <此 skill 目錄>/scripts/quality.py --source src -- tests

依專案調整來源目錄與測試參數，多個來源目錄可重複 --source。
--source 底下的所有 .py 都會檢查，包含測試沒有匯入的檔案。
報告寫在 .quality/report.json，.quality 目錄會自動排除於版控。

門檻沿用專案文件（如 AGENTS.md 或 CLAUDE.md）或使用者指定的值，沒有指定就用預設值；
不得自行修改門檻。

- 主要門檻是每個函式的覆蓋率（預設 80%）與循環複雜度（預設 10）。
- CRAP 用來排序修正的優先順序，報告依 CRAP 由高到低排列。
  另外兩項都通過時 CRAP 最多約 10.8，所以 --max-crap（預設 30）
  只有在放寬另外兩項門檻時才會起作用。

覆蓋率口徑是函式內的敘述覆蓋率，從未被呼叫的函式一律算 0%；
這是明確標示的替代口徑，不是基礎路徑覆蓋率。

首行函式另用 Python profiling hook 記錄實際入口，避免與模組入口混淆。
環境若已有 profiler 會回報錯誤；測試期間也不可覆寫 profiling hook。

以下函式會略過，列在報告的 skipped：

- stub：本體只有 docstring 或 `...`，例如 Protocol、@overload、抽象方法。
- excluded：被 coverage 的排除設定排除，例如 `# pragma: no cover`。

測試若在子行程執行被測程式碼（CLI 測試、pytest-xdist），
在專案的 pyproject.toml 加上以下設定，否則量不到：

    [tool.coverage.run]
    patch = ["subprocess"]

- 優先改善高複雜度且缺乏有效測試的函式。
- 不得以弱化斷言、刪除測試、任意排除程式碼來掩蓋失敗。
- 測試或規格有錯誤時，說明修正依據。

## 交付

回報修改行為、實際 Red／Green 指令與結果、品質報告位置、
超標項目、略過的函式及未完成的檢查。沒有執行就明示未執行。

最終品質檢查通過，不能單獨證明開發時遵循了 TDD。
