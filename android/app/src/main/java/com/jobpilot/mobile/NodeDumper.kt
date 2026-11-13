package com.jobpilot.mobile

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo

/**
 * 把当前界面的无障碍节点树拍平成可读文本。
 *
 * 为什么要这个：写选择器之前必须知道 BOSS 的真实节点长什么样。
 * 靠猜 viewId 写死，App 一次改版就全废；先抓树，再从树里找稳定的锚点
 * （id 优先，其次 className + 相对位置，最后才是文字）。
 */
object NodeDumper {

    private const val MAX_DEPTH = 25
    private const val MAX_NODES = 2000
    private const val MAX_TEXT = 40

    data class Dump(val text: String, val nodes: Int, val truncated: Boolean)

    fun dump(root: AccessibilityNodeInfo): String = walk(root).text

    fun walk(root: AccessibilityNodeInfo): Dump {
        val sb = StringBuilder(8 * 1024)
        val counter = intArrayOf(0)
        appendNode(root, 0, sb, counter)
        val truncated = counter[0] >= MAX_NODES
        if (truncated) sb.append("\n…（节点超过 ").append(MAX_NODES).append(" 个，已截断）")
        return Dump(sb.toString(), counter[0], truncated)
    }

    private fun appendNode(
        node: AccessibilityNodeInfo?,
        depth: Int,
        sb: StringBuilder,
        counter: IntArray,
    ) {
        if (node == null || depth > MAX_DEPTH || counter[0] >= MAX_NODES) return
        counter[0]++

        repeat(depth) { sb.append("  ") }

        val cls = node.className?.toString()?.substringAfterLast('.') ?: "View"
        sb.append(cls)

        val id = node.viewIdResourceName
        if (!id.isNullOrEmpty()) sb.append(" #").append(id.substringAfterLast('/'))

        val text = node.text?.toString()
        if (!text.isNullOrEmpty()) sb.append(" \"").append(clip(text)).append('"')

        val desc = node.contentDescription?.toString()
        if (!desc.isNullOrEmpty()) sb.append(" desc=\"").append(clip(desc)).append('"')

        val rect = Rect()
        node.getBoundsInScreen(rect)
        sb.append(" [").append(rect.left).append(',').append(rect.top).append(',')
            .append(rect.right).append(',').append(rect.bottom).append(']')

        if (node.isClickable) sb.append(" click")
        if (node.isEditable) sb.append(" edit")
        if (!node.isVisibleToUser) sb.append(" hidden")
        sb.append('\n')

        for (i in 0 until node.childCount) {
            appendNode(node.getChild(i), depth + 1, sb, counter)
        }
    }

    private fun clip(raw: String): String {
        val one = raw.replace('\n', ' ').trim()
        return if (one.length <= MAX_TEXT) one else one.substring(0, MAX_TEXT) + "…"
    }
}
