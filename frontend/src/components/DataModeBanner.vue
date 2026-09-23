<script setup lang="ts">
import { computed } from 'vue';
import type { ApiStatus, Evidence } from '@/types/api';
import { NON_OFFICIAL_LABEL, DISCLAIMER, dataModeHint, dataModeLabel, formatTime } from '@/types/view';
import { currentDataMode, isStaleEvidence } from '@/stores/dashboard';

const props = withDefaults(
  defineProps<{
    evidence?: Evidence[];
    warnings?: string[];
    status?: ApiStatus | null;
  }>(),
  { evidence: () => [], warnings: () => [], status: null },
);

const mode = computed(() => currentDataMode(props.evidence));
const modeLabel = computed(() => dataModeLabel(mode.value));
const modeHint = computed(() => dataModeHint(mode.value));
const stale = computed(() => isStaleEvidence(props.evidence));
const observedAt = computed(() => {
  for (let i = (props.evidence?.length ?? 0) - 1; i >= 0; i -= 1) {
    const value = props.evidence?.[i]?.observed_at;
    if (value) return formatTime(value);
  }
  return '未提供';
});
const freshness = computed(() => {
  for (let i = (props.evidence?.length ?? 0) - 1; i >= 0; i -= 1) {
    const value = props.evidence?.[i]?.freshness;
    if (value) return value;
  }
  return '未提供';
});
const modelVersion = computed(() => {
  for (let i = (props.evidence?.length ?? 0) - 1; i >= 0; i -= 1) {
    const value = props.evidence?.[i]?.model_version;
    if (value) return value;
  }
  return null;
});
</script>

<template>
  <section class="banner" :class="{ 'banner--stale': stale }" role="note">
    <div class="banner__row">
      <span class="banner__tag">历史回放/本地快照</span>
      <strong class="banner__mode">{{ modeLabel }}</strong>
      <span v-if="stale" class="banner__stale">数据陈旧</span>
      <span v-if="props.status" class="banner__status">响应状态：{{ props.status }}</span>
    </div>
    <p class="banner__hint">{{ modeHint }}</p>
    <dl class="banner__meta">
      <div>
        <dt>最新观测时间</dt>
        <dd>{{ observedAt }}</dd>
      </div>
      <div>
        <dt>数据新鲜度</dt>
        <dd>{{ freshness }}</dd>
      </div>
      <div>
        <dt>模型版本</dt>
        <dd>{{ modelVersion ?? '未提供' }}</dd>
      </div>
    </dl>
    <p class="banner__note">阈值与风险结果均为研究性口径，{{ NON_OFFICIAL_LABEL }}。</p>
    <p class="banner__disclaimer">{{ DISCLAIMER }}</p>
    <ul v-if="props.warnings && props.warnings.length > 0" class="banner__warnings">
      <li v-for="warning in props.warnings" :key="warning">{{ warning }}</li>
    </ul>
  </section>
</template>

<style scoped>
.banner {
  border: 1px solid #bfdbfe;
  border-left: 4px solid #1d4ed8;
  border-radius: 8px;
  padding: 12px 16px;
  background: #eff6ff;
  color: #1e3a8a;
}

.banner--stale {
  border-color: #fcd34d;
  border-left-color: #b45309;
  background: #fffbeb;
  color: #92400e;
}

.banner__row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.banner__tag {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #dbeafe;
  color: #1e40af;
}

.banner__mode {
  font-size: 16px;
}

.banner__stale {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #fde68a;
  color: #92400e;
}

.banner__status {
  font-size: 12px;
  color: #475569;
}

.banner__hint,
.banner__note,
.banner__disclaimer {
  margin: 6px 0 0;
  font-size: 13px;
  line-height: 1.6;
}

.banner__disclaimer {
  font-weight: 600;
}

.banner__meta {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  margin: 10px 0 0;
}

.banner__meta div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.banner__meta dt {
  font-size: 12px;
  color: #64748b;
}

.banner__meta dd {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.banner__warnings {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 12px;
}
</style>
