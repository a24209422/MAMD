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
});
window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));
showPage(location.hash.slice(1));
renderChrome();
pollState();
