// 啟動：先讀 token（會把網址裡的 token 拿掉），再畫跳轉列、換到網址指定的頁面，最後開始輪詢
app.token = readToken();
renderRail();
$('#railToggle').addEventListener('click', () => {
  const wide = $('#rail').classList.toggle('is-wide');
  $('#railToggle').setAttribute('aria-expanded', String(wide));
  $('#railToggle').title = wide ? '收合選單' : '展開選單';
});
window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));
showPage(location.hash.slice(1));
renderChrome();
pollState();
