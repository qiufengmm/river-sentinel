<script setup lang="ts">
import { computed } from 'vue';
import type { ApiStatus, RiskVerdict } from '@/types/api';
import {
  DISCLAIMER,
  NON_OFFICIAL_LABEL,
  RESEARCH_THRESHOLD_LABEL,
  formatMetric,
  formatTime,
  isRiskRenderable,
  riskLevelLabel,
} from '@/types/view';
import UnavailableState from './UnavailableState.vue';

const props = withDefaults(
  defineProps<{
    verdict?: RiskVerdict | null;
    status?: ApiStatus | null;
    /** 因缺少观测证据而跳过请求时的原因。 */
    skippedReason?: string | null;
  }>(),
  { verdict: null, status: null, skippedReason: null },
);

/**
 * 后端 RiskResult 没有 computable 字段：存在判定对象与等级即可展示。
 * 判定缺失时展示不可用，不补造等级。
 */
const renderable = computed(() => isRiskRenderable(props.verdict));
const levelClass = computed(() => `risk--${props.verdict?.level ?? 'unknown'}`);
const thresholdValue = computed(() => {
  const value = props.verdict?.threshold ?? null;
  if (value === null || value === undefined || Number.isNaN(value)) return '未提供';
  return `${value.toFixed(2)} ${props.verdict?.threshold_unit ?? 'm'}`;
});
const horizonText = computed(() => {
  const triggered = props.verdict?.triggered_horizons ?? [];
  return triggered.length > 0 ? triggered.map((h) => `h${h}`).join('、') : '无';
});
const missingText = computed(() => {
  const missing = props.verdict?.missing_horizons ?? [];
  return missing.length > 0 ? missing.map((h) => `h${h}`).join('、') : '无';
});
const predictedText = computed(() => {
  const values = props.verdict?.predicted_values ?? {};
  const entries = Object.entries(values);
  if (entries.length === 0) return '未提供预测值';
  return entries
    .sort((left, right) => Number(left[0]) - Number(right[0]))
    .map(([horizon, value]) => `h${horizon}=${formatMetric(value, 2)} m`)
    .join('、');
});
</script>

<template>
  <UnavailableState
    v-if="!renderable"
    status="unavailable"
    title="风险判定不可用"
    :reason="props.skippedReason ?? '未提供风险判定结果'"
    hint="确定性风险规则未返回结果，页面不展示任何替代风险等级。"
  />
  <section v-else class="risk" :class="levelClass" role="region" aria-label="研究性风险判定">
    <header class="risk__header">
      <h3 class="risk__title">研究性风险判定</h3>
      <span class="risk__level">{{ riskLevelLabel(props.verdict?.level) }}</span>
    </header>
    <dl class="risk__grid">
      <div>
        <dt>研究性高水位阈值</dt>
        <dd>{{ RESEARCH_THRESHOLD_LABEL }}：{{ thresholdValue }}</dd>
      </div>
      <div>
        <dt>阈值来源</dt>
        <dd>{{ props.verdict?.threshold_source || '未提供' }}</dd>
      </div>
      <div>
        <dt>等级来源</dt>
        <dd>{{ props.verdict?.basis_source || '未提供' }}（确定性规则，Provider 不得修改）</dd>
      </div>
      <div>
        <dt>预测值</dt>
        <dd>{{ predictedText }}</dd>
      </div>
      <div>
        <dt>触发时域</dt>
        <dd>{{ horizonText }}</dd>
      </div>
      <div>
        <dt>缺失不参评时域</dt>
        <dd>{{ missingText }}</dd>
      </div>
      <div>
        <dt>数据最后观测</dt>
        <dd>{{ formatTime(props.verdict?.last_observed_at) }}</dd>
      </div>
      <div>
        <dt>预测锚点</dt>
        <dd>{{ formatTime(props.verdict?.anchor_at) }}</dd>
      </div>
      <div v-if="props.verdict?.model || props.verdict?.model_version">
        <dt>模型版本</dt>
        <dd>{{ props.verdict?.model || '未提供' }} / {{ props.verdict?.model_version || '未提供' }}</dd>
      </div>
    </dl>

    <p class="risk__basis-title">判断依据（后端 basis 对象格式化结果）</p>
    <p class="risk__basis">{{ props.verdict?.basis_summary || '未提供判定依据' }}</p>

    <p v-if="props.verdict?.is_stale" class="risk__stale">该判定基于陈旧快照，请先核对数据时间。</p>
    <ul v-if="props.verdict?.limitations && props.verdict.limitations.length > 0" class="risk__limitations">
      <li v-for="item in props.verdict?.limitations ?? []" :key="item">{{ item }}</li>
    </ul>
    <p class="risk__note">本卡片为研究性口径，{{ NON_OFFICIAL_LABEL }}，不表示官方预警或超警信息。</p>
    <p class="risk__disclaimer">{{ DISCLAIMER }}</p>
  </section>
</template>

<style scoped>
.risk {
  border: 1px solid #e2e8f0;
  border-left: 4px solid #0f766e;
  border-radius: 8px;
  padding: 12px 16px;
  background: #f8fafc;
}

.risk--watch {
  border-left-color: #c8811a;
  background: #fffbeb;
}

.risk--elevated {
  border-left-color: #b91c1c;
  background: #fef2f2;
}

.risk__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.risk__title {
  margin: 0;
  font-size: 15px;
}

.risk__level {
  font-size: 13px;
  font-weight: 700;
  padding: 2px 10px;
  border-radius: 999px;
  background: #e2e8f0;
}

.risk__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px 20px;
  margin: 10px 0 0;
}

.risk__grid dt {
  font-size: 12px;
  color: #64748b;
}

.risk__grid dd {
  margin: 2px 0 0;
  font-size: 14px;
  line-height: 1.5;
}

.risk__basis-title {
  margin: 10px 0 0;
  font-size: 12px;
  color: #64748b;
}

.risk__basis {
  margin: 2px 0 0;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.risk__stale,
.risk__note,
.risk__disclaimer {
  margin: 8px 0 0;
  font-size: 13px;
  line-height: 1.6;
}

.risk__stale {
  color: #92400e;
  font-weight: 600;
}

.risk__disclaimer {
  font-weight: 600;
  color: #b91c1c;
}

.risk__limitations {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: #475569;
}
</style>
