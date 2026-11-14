package com.jobpilot.mobile

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.TextView

/**
 * v0.2 = 能真的打招呼。
 *
 * 首页只留一个主动作（开始 / 暂停）+ 两个数字（今日进度、累计）。抓取界面这套调试工具
 * 还在，但挪到下面 —— 它是我调选择器用的，不该占用户的第一屏。
 */
class MainActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var output: TextView
    private lateinit var progress: TextView
    private lateinit var btnStart: Button
    private lateinit var store: JobStore
    private var runner: BossRunner? = null

    private val dailyCap = 50

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        store = JobStore(applicationContext)

        status = findViewById(R.id.status)
        output = findViewById(R.id.output)
        progress = findViewById(R.id.progress)
        btnStart = findViewById(R.id.btn_start)

        btnStart.setOnClickListener { toggleRunner() }

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
                status.text = msg
                refreshProgress()
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

    override fun onResume() {
        super.onResume()
        val service = BossAccessibilityService.instance
        if (service == null) {
            status.text = getString(R.string.status_unknown)
        } else if (runner?.running != true) {
            status.text = "无障碍已连接，点「开始打招呼」"
        }
        refreshProgress()
    }

    override fun onDestroy() {
        runner?.stop("页面关闭")
        super.onDestroy()
    }
}
