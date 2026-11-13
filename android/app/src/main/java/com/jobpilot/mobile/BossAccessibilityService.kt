package com.jobpilot.mobile

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * v0.1 只做一件事：**在 BOSS 在前台的时候，把界面节点树缓存下来**。
 *
 * 为什么必须缓存，不能"点按钮时再抓"：
 * 用户切回我们的 App 时，前台窗口已经变成我们自己了，`rootInActiveWindow`
 * 返回的是这个调试页，不是 BOSS。所以抓取必须发生在 BOSS 前台的时刻——
 * 由无障碍事件驱动，节流后存进内存，主界面只是把缓存拿出来看。
 *
 * 这个坑不踩一遍很难想到，但它是"点了抓取却抓到自己的按钮"的全部原因。
 */
class BossAccessibilityService : AccessibilityService() {

    /** 抓取的快照。`source` 用来告诉用户这份数据是刚抓的，还是刚才缓存的。 */
    data class Snapshot(val text: String, val source: String, val atWallClock: Long)

    companion object {
        /** BOSS 直聘 Android 包名。写死而不是猜，实测为准。 */
        const val TARGET_PKG = "com.hpbr.bosszhipin"

        /** 事件风暴很密（列表滚动一次几十个事件），1.5 秒一次足够。 */
        private const val MIN_INTERVAL_MS = 1500L

        @Volatile
        var instance: BossAccessibilityService? = null
    }

    @Volatile
    private var cached: String = ""

    @Volatile
    private var cachedAtWall: Long = 0L

    private var lastDumpElapsed: Long = 0L

    @Volatile
    var lastPackage: String = ""
        private set

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val pkg = event?.packageName?.toString() ?: return
        lastPackage = pkg
        if (pkg != TARGET_PKG) return

        val now = SystemClock.elapsedRealtime()
        if (now - lastDumpElapsed < MIN_INTERVAL_MS) return

        val root: AccessibilityNodeInfo = rootInActiveWindow ?: return
        if (root.packageName?.toString() != TARGET_PKG) return

        lastDumpElapsed = now
        cached = NodeDumper.dump(root)
        cachedAtWall = System.currentTimeMillis()
    }

    override fun onInterrupt() {}

    override fun onUnbind(intent: Intent?): Boolean {
        instance = null
        cached = ""
        return super.onUnbind(intent)
    }

    /** 前台就是 BOSS 时抓实时；否则退回最近一次缓存。都没有返回 null。 */
    fun capture(): Snapshot? {
        val root = rootInActiveWindow
        if (root != null && root.packageName?.toString() == TARGET_PKG) {
            val text = NodeDumper.dump(root)
            cached = text
            cachedAtWall = System.currentTimeMillis()
            return Snapshot(text, "实时", cachedAtWall)
        }
        if (cached.isNotEmpty()) {
            return Snapshot(cached, "缓存于 ${agoText(cachedAtWall)}", cachedAtWall)
        }
        return null
    }

    private fun agoText(at: Long): String {
        if (at <= 0L) return "未知时间"
        val seconds = (System.currentTimeMillis() - at) / 1000
        return when {
            seconds < 60 -> "${seconds} 秒前"
            seconds < 3600 -> "${seconds / 60} 分钟前"
            else -> "${seconds / 3600} 小时前"
        }
    }
}
