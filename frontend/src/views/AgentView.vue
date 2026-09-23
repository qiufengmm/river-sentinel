<script setup lang="ts">
import { computed, ref } from 'vue';
import DataModeBanner from '@/components/DataModeBanner.vue';
import RiskCard from '@/components/RiskCard.vue';
import UnavailableState from '@/components/UnavailableState.vue';
import { useDashboardStore } from '@/stores/dashboard';
import { DISCLAIMER, formatTime } from '@/types/view';

const store = useDashboardStore();
const state = store.state;

const question = ref('');
const lastFailedMessage = ref<string | null>(null);

const failed = computed(() => state.status === 'unavailable' || state.status === 'error');
const hasAnswer = computed(() => Boolean(state.agent?.answer));

const presetQuestions = [
  '查询最近的水位历史回放数据',
  '给出未来 1、3、6 小时的预测结果',
  '对比各个模型的预测指标',
  '当前水位是否达到研究性高水位？',
  '生成一份水情简报',
];

async function sendQuestion(text?: string): Promise<void> {
  const message = (text ?? question.value).trim();
  if (!message) return;
  if (text) question.value = text;
  const response = await store.askAgent(message);
  lastFailedMessage.value =
    response.status === 'unavailable' || response.status === 'error'
      ? `智能体请求未成功：${response.warnings.join('；') || '未提供原因'}`
      : null;
}
</script>

<template>
  <div class="view">
    <header class="view__header">
      <h2>智能体对话</h2>
      <p class="view__desc">
        问题通过 <code>/api/v1/agent/chat</code> 提交，由后端 LangGraph 白名单工具编排。回答中的水位、预测值、风险等级全部来自工具结果，
        展示层不补充任何数值。
      </p>
    </header>

    <section class="presets">
      <span class="presets__label">示例问题</span>
      <button v-for="item in presetQuestions" :key="item" type="button" @click="sendQuestion(item)">{{ item }}</button>
    </section>

    <form class="ask" @submit.prevent="sendQuestion()">
      <textarea v-model="question" rows="3" placeholder="请输入关于历史水位、预测、模型对比或研究性风险的问题"></textarea>
      <button type="submit" :disabled="state.loading || question.trim().length === 0">
        {{ state.loading ? '请求中…' : '发送问题' }}
      </button>
    </form>

    <UnavailableState
      v-if="failed || lastFailedMessage"
      :status="state.status === 'unavailable' ? 'unavailable' : 'error'"
      title="智能体服务不可用"
      :reason="lastFailedMessage ?? state.error"
      hint="工具调用失败或后端不可达时，页面不生成替代回答、补造数值或防御建议。"
    />

    <DataModeBanner :evidence="state.evidence" :warnings="state.warnings" :status="state.status" />

    <UnavailableState v-if="!failed && !lastFailedMessage && !hasAnswer" status="empty" title="暂无回答" hint="请发送问题后查看智能体回答。" />

    <section v-if="hasAnswer" class="answer">
      <h3>回答</h3>
      <p class="answer__text">{{ state.agent?.answer }}</p>

      <div class="answer__block" v-if="state.agent?.intent">
        <h4>意图识别</h4>
        <p>{{ state.agent?.intent }}</p>
      </div>

      <div class="answer__block">
        <h4>工具调用摘要</h4>
        <ul v-if="state.agent && state.agent.tool_trace.length > 0">
          <li v-for="tool in state.agent?.tool_trace ?? []" :key="tool">{{ tool }}</li>
        </ul>
        <p v-else>本次回答未返回工具调用摘要。</p>
      </div>

      <div class="answer__block">
        <h4>证据</h4>
        <ul v-if="state.evidence.length > 0">
          <li v-for="(item, index) in state.evidence" :key="`${item.data_mode}-${index}`">
            数据模式：{{ item.data_mode }}｜观测时间：{{ formatTime(item.observed_at) }}｜新鲜度：{{ item.freshness ?? '未提供' }}｜模型版本：{{
              item.model_version ?? '未提供'
            }}｜数据源：{{ item.source_url ?? '未提供' }}
          </li>
        </ul>
        <p v-else>未返回证据条目。</p>
      </div>

      <div class="answer__block">
        <h4>风险判断依据</h4>
        <RiskCard :verdict="state.agent?.risk_verdict ?? null" :status="state.status" />
      </div>

      <div class="answer__block" v-if="state.agent?.evidence_summaries.length">
        <h4>证据摘要</h4>
        <ul>
          <li v-for="item in state.agent?.evidence_summaries ?? []" :key="item">{{ item }}</li>
        </ul>
      </div>

      <div class="answer__block" v-if="state.brief">
        <h4>简报</h4>
        <p class="answer__brief">
          {{ state.brief?.title }}｜数据时间：{{ formatTime(state.brief?.data_time) }}｜模型版本：{{
            state.brief?.model_version ?? '未提供'
          }}
        </p>
        <div v-for="section in state.brief?.sections ?? []" :key="section.heading" class="answer__section">
          <h5>{{ section.heading }}</h5>
          <ul>
            <li v-for="line in section.lines" :key="line">{{ line }}</li>
          </ul>
        </div>
      </div>
    </section>

    <p class="view__disclaimer">{{ DISCLAIMER }}</p>
  </div>
</template>

<style scoped>
.view__header h2 {
  margin: 0 0 4px;
  font-size: 18px;
}

.view__desc {
  margin: 0 0 12px;
  font-size: 13px;
  color: #475569;
  line-height: 1.6;
}

.presets {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 10px;
}

.presets__label {
  font-size: 12px;
  color: #475569;
}

.presets button {
  padding: 4px 10px;
  border: 1px solid #cbd5e1;
  border-radius: 999px;
  background: #f8fafc;
  font-size: 12px;
  cursor: pointer;
}

.ask {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}

.ask textarea {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  font-size: 13px;
  resize: vertical;
}

.ask button {
  padding: 8px 14px;
  border: none;
  border-radius: 6px;
  background: #1d4ed8;
  color: #ffffff;
  font-size: 13px;
  cursor: pointer;
}

.ask button:disabled {
  background: #94a3b8;
  cursor: not-allowed;
}

.answer {
  margin-top: 12px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 12px 16px;
  background: #ffffff;
}

.answer h3 {
  margin: 0 0 8px;
  font-size: 15px;
}

.answer__text {
  margin: 0;
  font-size: 14px;
  line-height: 1.8;
  white-space: pre-wrap;
}

.answer__block {
  margin-top: 12px;
}

.answer__block h4 {
  margin: 0 0 6px;
  font-size: 13px;
  color: #334155;
}

.answer__block ul {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.7;
}

.answer__block p {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
}

.answer__brief {
  white-space: pre-wrap;
}

.answer__section h5 {
  margin: 8px 0 4px;
  font-size: 13px;
  color: #334155;
}

.answer__section ul {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.7;
}

.view__disclaimer {
  margin: 12px 0 0;
  font-size: 13px;
  font-weight: 600;
  color: #b91c1c;
}
</style>
