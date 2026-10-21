// 按 PID 查找应用最大的可见窗口（CGWindowNumber），供 screencapture -l 使用
// 注意：kCGWindowOwnerName 是本地化名称（如"备忘录"），不能按英文名匹配，必须按 PID
import CoreGraphics
guard CommandLine.arguments.count > 1, let pid = Int32(CommandLine.arguments[1]) else { exit(0) }
let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as! [[String: Any]]
var best: (Int, CGFloat)? = nil
for w in list {
    guard let wp = w["kCGWindowOwnerPID"] as? Int32, wp == pid else { continue }
    guard let b = w["kCGWindowBounds"] as? [String: CGFloat] else { continue }
    guard (w["kCGWindowLayer"] as? Int) == 0 else { continue }
    let area = (b["Width"] ?? 0) * (b["Height"] ?? 0)
    if area > 10000 && (best == nil || area > best!.1) {
        best = (w["kCGWindowNumber"] as! Int, area)
    }
}
if let b = best { print(b.0) }
