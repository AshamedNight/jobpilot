package com.jobpilot.mobile

import android.content.Context
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.accessibility.AccessibilityNodeInfo
import java.util.Calendar
import kotlin.random.Random

/**
 * 选择器常量。全部来自真机 dump（dump.txt / dump-detail.txt），没有一个是我猜的。
 *
 * 注意 id 必须带包名前缀才能查：`com.hpbr.bosszhipin:id/recyclerView_list`。
 * dump 里显示成 `#recyclerView_list` 是我自己的 dumper 把前缀截掉了。
 */
object Sel {
    const val LIST = "recyclerView_list"
    const val TITLE = "tv_position_name"
    const val COMPANY = "tv_company_name"
    const val SALARY = "tv_salary_statue"
    const val CHAT_BTN = "btn_chat"
    const val BACK = "iv_back"
}

enum class Screen { LIST, DETAIL, CHAT, RISK, UNKNOWN }

/** 已打过招呼的岗位 + 今日计数。用 SharedPreferences，一台手机一个文件，够用且不用引数据库。 */
class JobStore(context: Context) {

    private val sp = context.getSharedPreferences("jobpilot", Context.MODE_PRIVATE)

    fun isDone(key: String): Boolean = sp.getBoolean("done_$key", false)

    fun markDone(key: String) {
        sp.edit().putBoolean("done_$key", true).apply()
    }

    fun totalDone(): Int = sp.all.keys.count { it.startsWith("done_") }

    fun todayCount(): Int = if (sp.getString("day", "") == today()) sp.getInt("today", 0) else 0

    fun incToday(): Int {
        val d = today()
        if (sp.getString("day", "") != d) {
            sp.edit().putString("day", d).putInt("today", 0).apply()
        }
        val next = sp.getInt("today", 0) + 1
        sp.edit().putInt("today", next).apply()
        return next
    }

    fun resetToday() {
        sp.edit().putString("day", today()).putInt("today", 0).apply()
    }

    private fun today(): String {
        val c = Calendar.getInstance()
        return "${c.get(Calendar.YEAR)}-${c.get(Calendar.MONTH) + 1}-${c.get(Calendar.DAY_OF_MONTH)}"
    }
}

/**
 * 打招呼引擎 v0.2。
 *
 * 一个 tick 只做一件事，然后排下一次 tick —— 不做 while 循环。
 * 理由：无障碍操作必须回到主线程、界面切换要等，用"状态机 + 定时器"比 sleep 循环稳得多，
 * 而且随时能 stop（用户按暂停不用等一个长循环跑完）。
 *
 * 状态判定完全靠界面特征，不靠"我现在应该在第几步"的假想状态：
 * 每 tick 重新看一眼当前屏幕是什么，再决定做什么。这样即使中间被弹窗打断、被人手点了一下，
 * 下一 tick 也能自己找回来。
 */
class BossRunner(
    private val service: BossAccessibilityService,
    private val store: JobStore,
    private val dailyCap: Int = 50,
    private val onStatus: (String) -> Unit,
) {

    private val handler = Handler(Looper.getMainLooper())

    @Volatile
    var running = false
        private set

    private var roundSent = 0
    private var roundLimit = Random.nextInt(3, 6)
    private var pendingKey: String? = null
    private var detailTries = 0
    private var chatTries = 0
    private var unknownTries = 0
    private var swipeStreak = 0
    private var needsBoss = 0

    /** 招呼语池。只在界面出现输入框时才用；BOSS 多数情况下会用账号设置好的招呼语自动发。 */
    private val messages = listOf(
        "您好，我在淄博，法务方向，方便的话想了解下这个岗位。",
        "您好，看到这个法务岗位比较匹配，想和您聊聊。",
        "您好，我在张店区，法务相关经验，期待进一步沟通。",
        "您好，对这个岗位很感兴趣，方便沟通一下吗？",
    )

    fun start() {
        if (running) return
        running = true
        roundSent = 0
        roundLimit = Random.nextInt(3, 6)
        pendingKey = null
        needsBoss = 0
        unknownTries = 0
        swipeStreak = 0
        status("启动中，今日已发 ${store.todayCount()}/$dailyCap")
        schedule(1500)
    }

    fun stop(reason: String) {
        running = false
        handler.removeCallbacksAndMessages(null)
        status("已停止：$reason")
    }

    // ------------------------------------------------------------------ tick

    private fun tick() {
        if (!running) return

        if (store.todayCount() >= dailyCap) {
            stop("今天已发满 $dailyCap 条")
            return
        }

        val root = service.bossRoot()
        if (root == null) {
            // 【2026-11-14 真机踩到】用户点「开始」时人在 JobPilot 界面，BOSS 根本不在前台。
            // 光提示是没用的 —— 要自己把它拉起来。但只拉 2 次：
            // 反复 startActivity 会一直打断用户，看起来像程序疯了。
            needsBoss++
            if (needsBoss <= 2) {
                val ok = service.launchBoss()
                status(if (ok) "正在打开 BOSS…（第 $needsBoss 次）" else "打不开 BOSS：请手动点「打开 BOSS」")
                schedule(2500)
            } else {
                status("等不到 BOSS：请手动切到 BOSS，再回来按「开始」")
                schedule(5000)
            }
            return
        }
        needsBoss = 0

        when (detect(root)) {
            Screen.RISK -> stop("命中平台提示（操作频繁/稍后再试），先停 30 分钟")
            Screen.LIST -> onList(root)
            Screen.DETAIL -> onDetail(root)
            Screen.CHAT -> onChat(root)
            Screen.UNKNOWN -> onUnknown()
        }
    }

    private fun detect(root: AccessibilityNodeInfo): Screen {
        if (hasRiskText(root)) return Screen.RISK
        if (root.byId(Sel.CHAT_BTN) != null) return Screen.DETAIL
        if (root.byId(Sel.LIST) != null) return Screen.LIST
        if (findEditable(root) != null) return Screen.CHAT
        if (root.findAccessibilityNodeInfosByText("发送")?.isNotEmpty() == true) return Screen.CHAT
        return Screen.UNKNOWN
    }

    // ------------------------------------------------------------------ 列表页

    private fun onList(root: AccessibilityNodeInfo) {
        pendingKey = null
        val list = root.byId(Sel.LIST)
        if (list == null) {
            retry("找不到岗位列表")
            return
        }

        val cards = visibleCards(list)
        if (cards.isEmpty()) {
            swipeOrStop("这一屏没有完整卡片")
            return
        }

        for (card in cards) {
            val title = card.byId(Sel.TITLE)?.text?.toString()?.trim().orEmpty()
            if (title.isEmpty()) continue
            val company = card.byId(Sel.COMPANY)?.text?.toString()?.trim().orEmpty()
            val key = keyOf(title, company)
            if (store.isDone(key)) continue

            pendingKey = key
            detailTries = 0
            chatTries = 0
            unknownTries = 0
            swipeStreak = 0

            val target = clickableSelfOrAncestor(card) ?: card
            if (!target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                status("点不开「$title」，跳过")
                store.markDone(key)
                schedule(1200)
                return
            }
            status("点开：$title")
            schedule(1800)
            return
        }

        swipeOrStop("这一屏都处理过了")
    }

    /**
     * 翻页。连续翻 4 屏都没有新岗位就停 —— 否则会一直划下去，
     * 既浪费动作，也让人误以为程序在干活。
     */
    private fun swipeOrStop(why: String) {
        swipeStreak++
        if (swipeStreak > 4) {
            swipeStreak = 0
            stop("翻到底了，没有更多新岗位（换个关键词或城市）")
            return
        }
        status("$why，上滑第 $swipeStreak 屏")
        service.swipeUp()
        schedule(2200)
    }

    /** 只取"完整露出来"的卡片 —— dump 里第 3 张是半截的，点它会点歪到举报按钮。 */
    private fun visibleCards(list: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val listRect = Rect()
        list.getBoundsInScreen(listRect)
        val out = ArrayList<AccessibilityNodeInfo>()

        for (i in 0 until list.childCount) {
            val child = list.getChild(i) ?: continue
            if (!child.isVisibleToUser) continue
            if (child.byId(Sel.TITLE) == null) continue
            val r = Rect()
            child.getBoundsInScreen(r)
            if (r.height() <= 0) continue
            if (r.bottom > listRect.bottom) continue
            out.add(child)
        }
        return out
    }

    // ------------------------------------------------------------------ 详情页

    private fun onDetail(root: AccessibilityNodeInfo) {
        val btn = root.byId(Sel.CHAT_BTN)
        if (btn == null) {
            detailTries++
            if (detailTries > 5) {
                back("详情页没加载出按钮，回列表")
                return
            }
            schedule(1200)
            return
        }

        val label = btn.text?.toString()?.trim().orEmpty()
        status("底部按钮：$label")

        when {
            label.contains("继续") -> {
                val key = pendingKey
                if (key != null) store.markDone(key)
                status("已沟通过，跳过")
                back(null)
            }

            label.contains("立即") -> {
                if (!btn.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                    back("按钮点不动")
                    return
                }
                status("已点「立即沟通」，等界面反应")
                schedule(2200)
            }

            else -> back("按钮文字不认识：$label")
        }
    }

    // ------------------------------------------------------------------ 点完之后

    private fun onChat(root: AccessibilityNodeInfo) {
        // 有输入框：填一句招呼语，再点「发送」
        val editable = findEditable(root)
        if (editable != null && chatTries == 0) {
            chatTries++
            val msg = messages[Random.nextInt(messages.size)]
            setText(editable, msg)
            val send = root.findAccessibilityNodeInfosByText("发送")?.firstOrNull()
            if (send != null) {
                (clickableSelfOrAncestor(send) ?: send).performAction(AccessibilityNodeInfo.ACTION_CLICK)
                status("已填招呼语并点发送")
            } else {
                status("填了招呼语，但没找到发送按钮")
            }
            schedule(1800)
            return
        }

        // 没有输入框：BOSS 用了账号自带的招呼语，点「立即沟通」就已经发出去了
        markSent("已发送")
        back(null)
    }

    /**
     * 界面既不是列表也不是详情 —— 大概率是聊天页（点了立即沟通之后），
     * 也可能是个没见过的弹窗。连着几次都认不出来，就当作"已经发出去了"并返回，
     * 不能无限停在这里。
     */
    private fun onUnknown() {
        unknownTries++
        if (pendingKey != null && unknownTries >= 3) {
            markSent("界面不认识，按已发送处理")
            back(null)
            return
        }
        if (unknownTries >= 6) {
            back("连续认不出界面，回列表重来")
            return
        }
        schedule(1500)
    }

    // ------------------------------------------------------------------ 收尾与节奏

    private fun markSent(reason: String) {
        val key = pendingKey
        if (key != null) store.markDone(key)
        pendingKey = null
        roundSent++
        val today = store.incToday()
        status("$reason · 今日 $today/$dailyCap · 本轮 $roundSent/$roundLimit")
    }

    private fun back(reason: String?) {
        if (reason != null) status(reason)
        service.back()
        pendingKey = null
        unknownTries = 0

        if (roundSent >= roundLimit) {
            roundSent = 0
            roundLimit = Random.nextInt(3, 6)
            val wait = Random.nextLong(5 * 60_000L, 10 * 60_000L)
            status("本轮结束，休息 ${wait / 60_000} 分钟")
            schedule(wait)
        } else {
            val wait = Random.nextLong(60_000L, 150_000L)
            status("下一条约 ${wait / 1000} 秒后")
            schedule(wait)
        }
    }

    private fun retry(msg: String) {
        status(msg)
        schedule(3000)
    }

    private fun schedule(ms: Long) {
        if (!running) return
        handler.removeCallbacksAndMessages(null)
        handler.postDelayed({ tick() }, ms)
    }

    private fun status(msg: String) {
        onStatus(msg)
    }

    // ------------------------------------------------------------------ 节点工具

    private fun keyOf(title: String, company: String): String =
        (title + "|" + company).hashCode().toString()

    private fun hasRiskText(root: AccessibilityNodeInfo): Boolean {
        val root_ = root
        val bad = listOf("操作频繁", "过于频繁", "请稍后再试", "系统繁忙")
        val hit = StringBuilder()
        walkText(root_, hit, 0)
        val all = hit.toString()
        return bad.any { all.contains(it) }
    }

    private fun walkText(node: AccessibilityNodeInfo?, sb: StringBuilder, depth: Int) {
        if (node == null || depth > 40) return
        node.text?.let { if (it.isNotEmpty()) sb.append(it).append('|') }
        for (i in 0 until node.childCount) {
            walkText(node.getChild(i), sb, depth + 1)
        }
    }

    private fun findEditable(root: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        if (root == null) return null
        if (root.isEditable) return root
        for (i in 0 until root.childCount) {
            val found = findEditable(root.getChild(i))
            if (found != null) return found
        }
        return null
    }

    private fun setText(node: AccessibilityNodeInfo, text: String) {
        val args = Bundle()
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }
}

/** `findAccessibilityNodeInfosByViewId` 必须带完整包名前缀，这里统一收口。 */
fun AccessibilityNodeInfo.byId(id: String): AccessibilityNodeInfo? {
    val full = "${BossAccessibilityService.TARGET_PKG}:id/$id"
    val found = findAccessibilityNodeInfosByViewId(full) ?: return null
    return found.firstOrNull()
}

/** 卡片本身通常就是可点的；万一不是，往上找最近的 clickable 祖先（最多 4 层）。 */
fun clickableSelfOrAncestor(node: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
    var cur = node
    var depth = 0
    while (cur != null && depth < 4) {
        if (cur.isClickable) return cur
        cur = cur.parent
        depth++
    }
    return null
}
