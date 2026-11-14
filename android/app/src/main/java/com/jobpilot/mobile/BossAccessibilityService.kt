package com.jobpilot.mobile

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
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

    // ------------------------------------------------ v0.2 自动化要用的动作

    /**
     * BOSS 的根节点。**不要用 rootInActiveWindow 直接判"BOSS 在不在"**。
     *
     * 【2026-11-14 真机踩到的】用户开分屏/小窗时，BOSS 明明就在屏幕上，
     * `rootInActiveWindow` 返回的却是 JobPilot 自己（因为我们刚被点过、是活动窗口）→
     * 引擎一直提示"请切到 BOSS 界面"，两次测试都卡在这里。
     *
     * 正确做法：先看活动窗口是不是 BOSS，不是就遍历 `windows`（需要
     * `flagRetrieveInteractiveWindows`）按包名找。分屏、小窗、悬浮窗都能找到。
     */
    fun bossRoot(): AccessibilityNodeInfo? {
        val active = rootInActiveWindow
        if (active?.packageName?.toString() == TARGET_PKG) return active

        val list = windows ?: return null
        for (w in list) {
            val r = w?.root ?: continue
            if (r.packageName?.toString() == TARGET_PKG) return r
        }
        return null
    }

    /** BOSS 窗口在屏幕上的位置。分屏时它只占半屏，滑动要按这块区域算，不能用整屏。 */
    fun bossWindowBounds(): Rect? {
        val list = windows ?: return null
        for (w in list) {
            val r = w?.root ?: continue
            if (r.packageName?.toString() != TARGET_PKG) continue
            val rect = Rect()
            w.getBoundsInScreen(rect)
            if (rect.width() > 0 && rect.height() > 0) return rect
        }
        return null
    }

    /** 把 BOSS 拉到前台。从 Service 启动 Activity 必须带 NEW_TASK，否则抛异常。 */
    fun launchBoss(): Boolean {
        val intent = packageManager.getLaunchIntentForPackage(TARGET_PKG) ?: return false
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        return try {
            startActivity(intent)
            true
        } catch (exc: Exception) {
            false
        }
    }

    /** 系统返回。比找 `#iv_back` 稳：弹窗打开时它会先关弹窗。 */
    fun back(): Boolean = performGlobalAction(GLOBAL_ACTION_BACK)

    /**
     * 上滑一屏。
     *
     * 用 dispatchGesture 而不是找滚动容器调 ACTION_SCROLL —— RecyclerView 的滚动节点
     * 在不同版本里位置不一样，而"手指从下往上划"这个动作永远有效。
     * 需要 accessibility_service_config 里 `canPerformGestures="true"`，漏了会返回 false。
     *
     * 坐标以 **BOSS 窗口**为准（不是整屏）：分屏时 BOSS 只占半屏，
     * 用整屏坐标划会划到 JobPilot 那头去。
     */
    fun swipeUp(): Boolean {
        val bounds = bossWindowBounds()
        val dm = resources.displayMetrics
        val top: Float
        val bottom: Float
        val x: Float
        if (bounds != null && bounds.height() > 0) {
            top = bounds.top.toFloat()
            bottom = bounds.bottom.toFloat()
            x = bounds.exactCenterX()
        } else {
            top = 0f
            bottom = dm.heightPixels.toFloat()
            x = dm.widthPixels / 2f
        }
        val span = bottom - top
        val path = Path().apply {
            moveTo(x, top + span * 0.78f)
            lineTo(x, top + span * 0.28f)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0L, 420L)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return dispatchGesture(gesture, null, null)
    }
}
