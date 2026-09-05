const q = new URLSearchParams(location.search);
const u = q.get("u") || "";
document.getElementById("url").textContent = u;
const why = q.get("why");
if (why === "shorts") document.getElementById("why").textContent = "YouTube Shorts đã bị tắt.";
if (why === "channel") {
  document.getElementById("title").textContent = "Kênh YouTube này chưa được duyệt";
  document.getElementById("why").textContent = "Chỉ xem được video từ các kênh trong danh sách cho phép.";
}
