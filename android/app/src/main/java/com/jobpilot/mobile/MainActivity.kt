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
 * v0.1 = 节点探针。
 *
 * 它不做自动化，只负责三件事：把无障碍打开、跳去 BOSS、把 BOSS 的界面节点树导出来。
 * 为什么先做这个：写点击规则之前必须先看清真实节点，否则只能靠猜 id，
 * 一个 App 改版就全线失效。这一步花半天，能省掉后面三天的反复试错。
 */
class MainActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var output: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        status = findViewById(R.id.status)
        output = findViewById(R.id.output)

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
    }

    override fun onResume() {
        super.onResume()
        val service = BossAccessibilityService.instance
        status.text = when {
            service == null -> getString(R.string.status_unknown)
            service.lastPackage.isEmpty() -> "无障碍：已连接"
            else -> "无障碍：已连接（最近窗口 ${service.lastPackage}）"
        }
    }
}
