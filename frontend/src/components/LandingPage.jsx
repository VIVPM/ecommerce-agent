import React from 'react';
import { Sparkles, Zap, ShieldCheck, Brain, ArrowRight, ShoppingBag } from 'lucide-react';
import './landing.css';

const features = [
  {
    icon: Sparkles,
    title: 'Agentic routing',
    body: 'Ask in plain language. The agent decides whether to search the product catalog or answer from store policies — no menus, no filters.',
  },
  {
    icon: Zap,
    title: 'Streaming answers',
    body: 'Responses stream in as they are written, with live progress — no staring at a spinner waiting for the whole reply.',
  },
  {
    icon: ShieldCheck,
    title: 'Grounded & safe',
    body: 'Product answers come from a read-only database, so prices and results are real — never invented inventory.',
  },
  {
    icon: Brain,
    title: 'Remembers context',
    body: 'Follow-ups just work. "Any cheaper?" or "what about Nike?" resolve against the conversation automatically.',
  },
];

const steps = [
  { n: '01', title: 'Ask', body: 'Type a question about a product or a store policy.' },
  { n: '02', title: 'Route & retrieve', body: 'The agent picks the right tool and pulls grounded data.' },
  { n: '03', title: 'Streamed answer', body: 'A clear, sourced answer streams straight back to you.' },
];

const LandingPage = ({ onGetStarted, onSignIn }) => {
  return (
    <div className="landing">
      <nav className="l-nav">
        <div className="l-container l-nav-inner">
          <button className="l-brand" onClick={onGetStarted}>
            <ShoppingBag size={18} />
            <span>Ecommerce Agent</span>
          </button>
          <div className="l-nav-actions">
            <button className="l-btn l-btn-ghost" onClick={onSignIn}>Sign in</button>
            <button className="l-btn l-btn-primary" onClick={onGetStarted}>Get started</button>
          </div>
        </div>
      </nav>

      <header className="l-container l-hero">
        <div className="l-eyebrow">AI SHOPPING ASSISTANT</div>
        <h1 className="l-display">
          Shop smarter.<br />Just ask.
        </h1>
        <p className="l-lead">
          One message finds the right product or answers a store question — routed by an
          AI agent, grounded in real data, and streamed back in real time.
        </p>
        <div className="l-hero-cta">
          <button className="l-btn l-btn-primary l-btn-lg" onClick={onGetStarted}>
            Get started <ArrowRight size={16} />
          </button>
          <button className="l-btn l-btn-secondary l-btn-lg" onClick={onSignIn}>
            Sign in
          </button>
        </div>

        <div className="l-mock">
          <div className="l-mock-bar">
            <span className="l-dot" />
            <span className="l-dot" />
            <span className="l-dot" />
            <span className="l-mock-title">Ecommerce Assistant</span>
          </div>
          <div className="l-mock-body">
            <div className="l-msg l-msg-user">Show me running shoes under ₹2000</div>
            <div className="l-msg l-msg-bot">
              <div>Here are the top results from your search:</div>
              <div className="l-prod">1. Campus Women Running Shoes — ₹1,104 (35% off) · ★ 4.4</div>
              <div className="l-prod">2. Sparx Men Running Shoes — ₹1,499 (25% off) · ★ 4.3</div>
              <span className="l-caret" />
            </div>
          </div>
        </div>
      </header>

      <section className="l-container l-section">
        <div className="l-section-head">
          <div className="l-eyebrow">WHY IT'S DIFFERENT</div>
          <h2 className="l-h2">A shopping assistant that actually reasons.</h2>
        </div>
        <div className="l-grid">
          {features.map((f) => (
            <div className="l-card" key={f.title}>
              <div className="l-card-icon"><f.icon size={18} /></div>
              <h3 className="l-card-title">{f.title}</h3>
              <p className="l-card-body">{f.body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="l-container l-section">
        <div className="l-section-head">
          <div className="l-eyebrow">HOW IT WORKS</div>
          <h2 className="l-h2">Three steps, one message.</h2>
        </div>
        <div className="l-steps">
          {steps.map((s) => (
            <div className="l-step" key={s.n}>
              <div className="l-step-n">{s.n}</div>
              <h3 className="l-card-title">{s.title}</h3>
              <p className="l-card-body">{s.body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="l-container l-cta-wrap">
        <div className="l-cta">
          <h2 className="l-h2">Ready to try it?</h2>
          <p className="l-lead l-cta-lead">Create an account and start asking in seconds.</p>
          <button className="l-btn l-btn-primary l-btn-lg" onClick={onGetStarted}>
            Get started <ArrowRight size={16} />
          </button>
        </div>
      </section>

      <footer className="l-footer">
        <div className="l-container l-footer-inner">
          <div className="l-brand l-brand-static">
            <ShoppingBag size={16} />
            <span>Ecommerce Agent</span>
          </div>
          <span className="l-foot-meta">Powered by Gemini · React + FastAPI</span>
        </div>
      </footer>
    </div>
  );
};

export default LandingPage;
