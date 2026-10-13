package com.qiubo.optimaltv.ui.player

/** Keep original line indices: selecting a provider must not change switchLine's index. */
internal object PlayerSourceGroupingPolicy {
    data class Group(val provider: String, val lineIndices: List<Int>)

    fun groups(labels: List<String>, sourceRefs: List<String> = emptyList()): List<Group> {
        val groups = linkedMapOf<String, MutableList<Int>>()
        labels.forEachIndexed { index, label ->
            val separator = label.indexOfFirst { it == '·' || it == '•' || it == '・' }
            val ref = sourceRefs.getOrNull(index).orEmpty().substringBefore('|')
            val fallback = if (ref.startsWith("hhkan:") || ref.startsWith("hhkanplay:") ||
                (sourceRefs.getOrNull(index).orEmpty().contains('|') && ref.toLongOrNull() != null)) "好好看" else "其他信号源"
            val provider = if (separator > 0) label.substring(0, separator).trim() else fallback
            groups.getOrPut(provider.ifBlank { "其他信号源" }) { mutableListOf() }.add(index)
        }
        return groups.map { (provider, indices) -> Group(provider, indices.toList()) }
    }
}
