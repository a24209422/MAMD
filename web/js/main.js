// 啟動：本頁的專案已在 core 載入時由網址固定；先讀 token（會把網址裡的 token 拿掉），再畫跳轉列、接上開啟資料夾，
// 換到網址指定的頁面，最後才開始輪詢，所有請求一開始就送到這個專案
app.token = readToken();
renderRail();
$('#railToggle').addEventListener('click', () => {
  const wide = $('#rail').classList.toggle('is-wide');
  $('#railToggle').setAttribute('aria-expanded', String(wide));
  $('#railToggle').title = wide ? '收合選單' : '展開選單';
});
$('#projectOpen').addEventListener('click', () => openProjectDialog());
$('#projectCancel').addEventListener('click', () => $('#projectDialog').close());
$('#projectForm').addEventListener('submit', event => {
  event.preventDefault();
  openProject($('#projectPathInput').value);
});
document.addEventListener('click', event => {
  if (event.target.closest('[data-project-reopen]')) openProjectDialog(missingProjectPath());
  const syncButton = event.target.closest('[data-sync-take]');
  if (syncButton) takeSyncSide(syncButton.dataset.syncTake);
});

// 選哪一邊會替換另一邊的進度，要使用者再確認一次；結果整段顯示，失敗時才知道要先處理什麼
async function takeSyncSide(side) {
  const label = side === 'local' ? '保留本機進度並產生新的交接版本' : '改接續交接檔的進度';
  if (!window.confirm(`確定要${label}？被替換的進度會先備份到 .board/sync-backups`)) return;
  const result = await apiPost(`/api/sync/${side}`);
  window.alert(result.message || (result.ok ? '已完成' : '失敗'));
  pollState();
}
window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));
showPage(location.hash.slice(1));
renderChrome();
pollState();
