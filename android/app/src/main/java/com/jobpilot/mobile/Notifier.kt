package com.jobpilot.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import android.os.Build
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 常驻通知：BOSS 全屏在跑的时候，下拉通知栏就能看到进度，不用切回 JobPilot。
 *
 * 为什么用通知而不是悬浮窗：
 * - 悬浮窗要「显示在其他应用上层」权限，还得用户去设置里开
 * - 悬浮窗会盖住 BOSS 的按钮（我们的点击是按节点算的，但用户手动操作会被挡）
 * - 「无障碍 + 悬浮窗」是自动化工具的典型组合特征，能不加就不加
 *
 * minSdk 是 26，所以直接用 `Notification.Builder(context, channelId)`，不需要 androidx 兼容包。
 */
class Notifier(private val context: Context) {

    companion object {
        private const val CHANNEL_ID = "jobpilot_status"
        private const val NOTIFICATION_ID = 1001
    }

    private var channelReady = false
    private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    private fun ensureChannel() {
        if (channelReady) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val channel = NotificationChannel(
                CHANNEL_ID,
                "运行状态",
                NotificationManager.IMPORTANCE_LOW, // LOW：不出声、不弹横幅，只占一行
            ).apply {
                description = "显示打招呼进度与下一次动作时间"
                setShowBadge(false)
                enableVibration(false)
            }
            nm.createNotificationChannel(channel)
        }
        channelReady = true
    }

    /**
     * 更新通知。`nextTickAt` 用**绝对时间**而不是"还剩多少秒"——
     * 通知不会每秒刷新，写相对秒数会立刻变成假数字（屏幕上那个倒计时负责实时跳动）。
     */
    fun update(status: String, nextTickAt: Long, today: Int, cap: Int) {
        ensureChannel()
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

        val openApp = PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        val pause = PendingIntent.getBroadcast(
            context,
            1,
            Intent(context, PauseReceiver::class.java).setAction(PauseReceiver.ACTION_PAUSE),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        val text = buildString {
            append(status)
            if (nextTickAt > System.currentTimeMillis()) {
                append("  ·  下一条 ")
                append(timeFormat.format(Date(nextTickAt)))
            }
        }

        val builder = Notification.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("今日 $today/$cap")
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setOngoing(true)          // 划不掉，提醒它还活着
            .setOnlyAlertOnce(true)    // 刷新时不重复提示
            .setShowWhen(false)
            .setContentIntent(openApp)
            .addAction(
                Notification.Action.Builder(
                    // 用 Icon 对象而不是资源 id：`Builder(int,...)` 在 API 23 起已弃用，
                    // 而传 null 在部分 ROM 上会让动作按钮整块消失。
                    Icon.createWithResource(context, R.drawable.ic_launcher_foreground),
                    "暂停",
                    pause,
                ).build()
            )

        nm.notify(NOTIFICATION_ID, builder.build())
    }

    fun clear() {
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.cancel(NOTIFICATION_ID)
    }
}
