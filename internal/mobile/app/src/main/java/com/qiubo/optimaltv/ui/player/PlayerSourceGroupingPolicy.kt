package com.qiubo.optimaltv.ui.player

import com.qiubo.optimaltv.data.model.LineInfo

/** Keeps each grouped source's callback index aligned with the original playback list. */
internal object PlayerSourceGroupingPolicy {
    data class Group(val provider: String, val lineIndices: List<Int>)

    fun groups(lines: List<LineInfo>): List<Group> {
        val grouped = linkedMapOf<String, MutableList<Int>>()
        lines.forEachIndexed { index, line ->
            val label = line.label
            val separator = separatorIndex(label)
            val explicitProvider = if (separator > 0) {
                label.substring(0, separator).trim().takeIf(String::isNotBlank)
            } else null
            val provider = explicitProvider
                ?: if (hasHhkanReference(line)) HHKAN_PROVIDER
                else OTHER_PROVIDER
            grouped.getOrPut(provider.ifBlank { OTHER_PROVIDER }) { mutableListOf() }.add(index)
        }
        return grouped.map { (provider, indices) -> Group(provider, indices.toList()) }
    }

    fun displayLabel(label: String, provider: String): String {
        val separator = separatorIndex(label)
        if (separator < 0 || label.substring(0, separator).trim() != provider) return label
        return label.substring(separator + 1).trim().ifBlank { label }
    }

    private fun separatorIndex(label: String): Int =
        label.indexOfFirst { it == '·' || it == '•' || it == '・' }

    private fun hasHhkanReference(line: LineInfo): Boolean =
        sequenceOf(line.vodId, line.playRef).any { reference ->
            reference.trim()
                .substringBefore('|')
                .substringBefore(':')
                .equals("hhkan", ignoreCase = true)
        }

    private const val HHKAN_PROVIDER = "好好看"
    private const val OTHER_PROVIDER = "其他信号源"
}
