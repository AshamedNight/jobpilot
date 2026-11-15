package com.jobpilot.mobile

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.widget.Button
import android.widget.TextView

/**
 * v0.4：界面 + 实时倒计时 + 常驻通知。
 *
 * 首页只留一个主动作（开始 / 暂停）+ 两个数字（今日进度、累计）。抓取界面这套调试工具
 * 还在，但挪到下面 —— 它是我调选择器用的，不该占用户的第一屏。
 */
class MainActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var countdown: TextView
    private lateinit var output: TextView
    private lateinit var progress: TextView
    private lateinit var btnStart: Button
    private lateinit var store: JobStore
    private var runner: BossRunner? = null

    private val dailyCap = 50
    private val uiHandler = Handler(Looper.getMainLooper())

    /**
     * 每 500ms 刷一次倒计时。
     *
     * 为什么不是每 1000ms：秒数跳变的边界和屏幕刷新不同步，看起来会"卡半拍"；
     * 500ms 足够顺滑，开销也可以忽略（只改一个 TextView 的文字）。
     */
    private val ticker = object : Runnable {
        override fun run() {
            val r = runner
            if (r != null && r.running && r.nextTickAt > 0L) {
                val left = (r.nextTickAt - System.currentTimeMillis()).coerceAtLeast(0L)
                countdown.text = "下一条 %d:%02d".format(left / 60_000L, (left / 1000L) % 60)
            } else if (r != null && r.running) {
                countdown.text = "正在执行…"
            } else {
                countdown.text = ""
            }
            uiHandler.postDelayed(this, 500L)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        store = JobStore(applicationContext)

        status = findViewById(R.id.status)
        countdown = findViewById(R.id.countdown)
        output = findViewById(R.id.output)
        progress = findViewById(R.id.progress)
        btnStart = findViewById(R.id.btn_start)

        btnStart.setOnClickListener { toggleRunner() }

        // Android 13+ 通知要用户明确授权。没授权也不会崩，只是通知不显示 ——
        // 而「通知栏看进度」正是这个版本的主要卖点，所以启动时问一次。
        ensureNotificationPermission()

        findViewById<Button>(R.id.btn_accessibility).setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }

        findViewById<Button>(R.id.btn_boss).setOnClickListener {
            val launch = packageManager.getLaunchIntentForPackage(BossAccessibilityService.TARGET_PKG)
            if (launch == null) {
                status.text = "没找到 BOSS 直聘（${BossAccessibilityService.TARGET_PKG}），先装上"
            } else {
                startActivity(launch)
            }
        }

        findViewById<Button>(R.id.btn_dump).setOnClickListener {
            val service = BossAccessibilityService.instance
            if (service == null) {
                status.text = "无障碍没开：点「开无障碍」→ 找到 JobPilot → 打开"
                return@setOnClickListener
            }
            val snapshot = service.capture()
            if (snapshot == null) {
                status.text = "还没抓到：先去 BOSS 搜索结果页滑一下，再回来点这里"
                return@setOnClickListener
            }
            output.text = snapshot.text
            status.text = "抓到 ${snapshot.text.lines().size} 行（来源：${snapshot.source}）"
        }

        findViewById<Button>(R.id.btn_copy).setOnClickListener {
            val content = output.text
            if (content.isNullOrBlank()) {
                status.text = "还没有内容可复制"
                return@setOnClickListener
            }
            val clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
            clipboard.setPrimaryClip(ClipData.newPlainText("jobpilot-dump", content))
            status.text = "已复制 ${content.length} 个字符"
        }

        refreshProgress()
    }

    private fun toggleRunner() {
        val service = BossAccessibilityService.instance
        if (service == null) {
            status.text = "无障碍没开：先点「开无障碍」，在列表里打开 JobPilot"
            return
        }

        val current = runner
        if (current != null && current.running) {
            current.stop("手动暂停")
            refreshProgress()
            return
        }

        val fresh = BossRunner(service, store, dailyCap) { msg ->
            runOnUiThread {
                // 通知栏的「暂停」可以在 Activity 已经销毁后触发 stop()，
                // 那时候再碰 View 是未定义行为。这里必须挡一下。
                if (!isFinishing && !isDestroyed) {
                    status.text = msg
                    refreshProgress()
                }
            }
        }
        runner = fresh

        // 【2026-11-14】点「开始」时人肯定在 JobPilot 界面，BOSS 大概率不在前台。
        // 这里立刻把 BOSS 拉到前台（而不是等引擎 1.5 秒后自己拉），用户点完马上能看到动作开始。
        // 如果 BOSS 已经在分屏/小窗里可见，bossRoot() 非空，就不打扰用户。
        if (service.bossRoot() == null) {
            service.launchBoss()
        }

        fresh.start()
        refreshProgress()
    }

    private fun refreshProgress() {
        progress.text = "今日 ${store.todayCount()}/$dailyCap · 累计已打 ${store.totalDone()}"
        btnStart.text = if (runner?.running == true) {
            getString(R.string.btn_stop)
        } else {
            getString(R.string.btn_start)
        }
    }

    private fun ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        if (!granted) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1001)
        }
    }

    override fun onResume() {
        super.onResume()
        val service = BossAccessibilityService.instance
        if (service == null) {
            status.text = getString(R.string.status_unknown)
        } else if (runner?.running != true) {
            status.text = "无障碍已连接，点「开始打招呼」"
        }
        refreshProgress()
        uiHandler.removeCallbacks(ticker)
        uiHandler.post(ticker)
    }

    override fun onPause() {
        uiHandler.removeCallbacks(ticker)
        super.onPause()
    }

    override fun onDestroy() {
        uiHandler.removeCallbacks(ticker)
        runner?.stop("页面关闭")
        super.onDestroy()
    }
}
