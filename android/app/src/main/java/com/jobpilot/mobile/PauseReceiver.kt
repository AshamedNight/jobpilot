package com.jobpilot.mobile

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * 通知栏那个「暂停」按钮的落点。
 *
 * 为什么走广播而不是直接 startActivity：用户按暂停时人在别的界面（BOSS 全屏），
 * 把他拽回 JobPilot 才算暂停就太粗暴了。广播不进 UI，直接停引擎。
 *
 * 依赖 `BossRunner.active` 这个静态引用——引擎本身不持有 Activity，
 * 所以这里不会造成内存泄漏（持有的是 service + store）。
 */
class PauseReceiver : BroadcastReceiver() {

    companion object {
        const val ACTION_PAUSE = "com.jobpilot.mobile.PAUSE"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_PAUSE) return
        BossRunner.active?.stop("从通知栏暂停")
        Notifier(context.applicationContext).clear()
    }
}
