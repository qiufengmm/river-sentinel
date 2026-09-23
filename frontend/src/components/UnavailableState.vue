<script setup lang="ts">
import { computed } from 'vue';
import type { ApiStatus } from '@/types/api';
import { DISCLAIMER, statusLabel } from '@/types/view';

export type UnavailableKind = ApiStatus | 'empty';

const props = withDefaults(
  defineProps<{
    status?: UnavailableKind;
    title?: string | null;
    reason?: string | null;
    hint?: string | null;
    showDisclaimer?: boolean;
  }>(),
  {
    status: 'unavailable',
    title: null,
    reason: null,
    hint: null,
    showDisclaimer: true,
  },
);

const DEFAULT_TITLES: Record<UnavailableKind, string> = {
  ok: '暂无内容',
  degraded: '结果已降级',
  unavailable: '服务或数据不可用',
  error: '接口请求失败',
  empty: '暂无数据',
};

const DEFAULT_HINTS: Record<UnavailableKind, string> = {
  ok: '暂无可展示内容。',
  degraded: '已回退到已验证的基线结果，请查看降级原因。',
  unavailable: '后端服务或数据不可用，页面不展示任何替代数值。',
  error: '接口请求失败或网络中断，页面不展示任何替代数值。',
  empty: '当前查询没有返回数据，页面不展示任何替代数值。',
};

const resolvedTitle = computed(() => props.title ?? DEFAULT_TITLES[props.status]);
const resolvedHint = computed(() => props.hint ?? DEFAULT_HINTS[props.status]);
const badgeText = computed(() => (props.status === 'empty' ? '空数据' : statusLabel(props.status)));
</script>

<template>
  <section class="unavailable" role="status" aria-live="polite">
    <header class="unavailable__header">
      <span class="unavailable__badge" :class="`unavailable__badge--${props.status}`">{{ badgeText }}</span>
      <h3 class="unavailable__title">{{ resolvedTitle }}</h3>
    </header>
    <p class="unavailable__hint">{{ resolvedHint }}</p>
    <p v-if="props.reason" class="unavailable__reason">原因：{{ props.reason }}</p>
    <p v-if="props.showDisclaimer" class="unavailable__disclaimer">{{ DISCLAIMER }}</p>
    <slot />
  </section>
</template>

<style scoped>
.unavailable {
  border: 1px dashed #c2410c;
  border-radius: 8px;
  padding: 12px 16px;
  background: #fff7ed;
  color: #7c2d12;
}

.unavailable__header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.unavailable__badge {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #fed7aa;
  color: #7c2d12;
}

.unavailable__badge--error {
  background: #fecaca;
  color: #991b1b;
}

.unavailable__badge--empty {
  background: #e5e7eb;
  color: #374151;
}

.unavailable__badge--degraded {
  background: #fef08a;
  color: #854d0e;
}

.unavailable__title {
  margin: 0;
  font-size: 15px;
}

.unavailable__hint,
.unavailable__reason {
  margin: 6px 0 0;
  font-size: 13px;
  line-height: 1.6;
}

.unavailable__disclaimer {
  margin: 8px 0 0;
  font-size: 12px;
  color: #92400e;
}
</style>
