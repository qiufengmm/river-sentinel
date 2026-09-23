<script setup lang="ts">
import { computed, ref } from 'vue';
import AgentView from '@/views/AgentView.vue';
import DashboardView from '@/views/DashboardView.vue';
import ForecastView from '@/views/ForecastView.vue';
import HistoryView from '@/views/HistoryView.vue';
import { DISCLAIMER } from '@/types/view';
import type { ViewKey } from '@/types/view';

const NAV_ITEMS: { key: ViewKey; label: string }[] = [
  { key: 'dashboard', label: '总览' },
  { key: 'history', label: '历史回放' },
  { key: 'forecast', label: '预测与模型对比' },
  { key: 'agent', label: '智能体对话' },
];

const active = ref<ViewKey>('dashboard');
const activeComponent = computed(() => {
  switch (active.value) {
    case 'history':
      return HistoryView;
    case 'forecast':
      return ForecastView;
    case 'agent':
      return AgentView;
    default:
      return DashboardView;
  }
});
</script>

<template>
  <div class="app">
    <header class="app__header">
      <div class="app__brand">
        <h1>River Sentinel</h1>
        <p>飞云江水位预测与防汛辅助决策智能体系统（毕业设计演示）</p>
      </div>
      <p class="app__disclaimer">{{ DISCLAIMER }}</p>
    </header>

    <nav class="app__nav">
      <button
        v-for="item in NAV_ITEMS"
        :key="item.key"
        type="button"
        :class="{ 'app__nav-item--active': active === item.key }"
        class="app__nav-item"
        @click="active = item.key"
      >
        {{ item.label }}
      </button>
    </nav>

    <main class="app__main">
      <component :is="activeComponent" />
    </main>

    <footer class="app__footer">
      <p>数据来源：温州市公共数据开放平台“文成县飞云江二期治理工程水位信息”数据集（历史回放演示）。</p>
      <p>{{ DISCLAIMER }}；研究性高水位阈值为非官方警戒标准。</p>
    </footer>
  </div>
</template>

<style scoped>
.app {
  max-width: 1120px;
  margin: 0 auto;
  padding: 20px 16px 40px;
}

.app__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  flex-wrap: wrap;
}

.app__brand h1 {
  margin: 0;
  font-size: 20px;
}

.app__brand p {
  margin: 4px 0 0;
  font-size: 13px;
  color: #475569;
}

.app__disclaimer {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: #b91c1c;
}

.app__nav {
  display: flex;
  gap: 8px;
  margin: 16px 0;
  flex-wrap: wrap;
}

.app__nav-item {
  padding: 6px 14px;
  border: 1px solid #cbd5e1;
  border-radius: 999px;
  background: #ffffff;
  font-size: 13px;
  cursor: pointer;
}

.app__nav-item--active {
  background: #1d4ed8;
  border-color: #1d4ed8;
  color: #ffffff;
}

.app__footer {
  margin-top: 24px;
  font-size: 12px;
  color: #64748b;
  line-height: 1.7;
}

.app__footer p {
  margin: 0;
}
</style>
