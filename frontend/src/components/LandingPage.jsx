import React, { useEffect, useRef, useState } from 'react';
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

const prefersReducedMotion = () =>
  typeof window !== 'undefined' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// Fades + rises its children into view the first time they're scrolled to.
function Reveal({ children, className = '' }) {
  const ref = useRef(null);
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (prefersReducedMotion()) { setShown(true); return; }
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) { setShown(true); io.disconnect(); }
      },
      { threshold: 0.15, rootMargin: '0px 0px -8% 0px' },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return (
    <div ref={ref} className={`l-reveal ${shown ? 'is-visible' : ''} ${className}`}>
      {children}
    </div>
  );
}

const DEMO_Q = 'Show me running shoes under ₹2000';
const DEMO_A = [
  'Here are the top results from your search:',
  '1. Campus Women Running Shoes — ₹1,104 · ★ 4.4 (13,814)',
  '2. Sparx Men Running Shoes — ₹1,499 · ★ 4.3 (8,921)',
].join('\n');

// Types the question, pauses to "think", streams the answer, holds, then loops —
// the same ask -> stream shape as the real chat.
function ChatDemo() {
  const [q, setQ] = useState('');
  const [a, setA] = useState('');
  const [phase, setPhase] = useState('typing'); // typing | thinking | streaming | done

  useEffect(() => {
    if (prefersReducedMotion()) { setQ(DEMO_Q); setA(DEMO_A); setPhase('done'); return; }
    let cancelled = false;
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const run = async () => {
      while (!cancelled) {
        setQ(''); setA(''); setPhase('typing');
        for (let i = 1; i <= DEMO_Q.length && !cancelled; i++) { setQ(DEMO_Q.slice(0, i)); await wait(38); }
        if (cancelled) return;
        setPhase('thinking'); await wait(750);
        if (cancelled) return;
        setPhase('streaming');
        for (let i = 1; i <= DEMO_A.length && !cancelled; i++) { setA(DEMO_A.slice(0, i)); await wait(16); }
        if (cancelled) return;
        setPhase('done'); await wait(2800);
      }
    };
    run();
    return () => { cancelled = true; };
  }, []);

  const lines = a.split('\n');
  return (
    <div className="l-mock-body">
      <div className="l-msg l-msg-user">
        {q || ' '}
        {phase === 'typing' && <span className="l-caret" />}
      </div>
      {phase !== 'typing' && (
        <div className="l-msg l-msg-bot">
          {phase === 'thinking' ? (
            <span className="l-thinking">Searching products<span className="l-caret" /></span>
          ) : (
            lines.map((line, i) => {
              const isLast = i === lines.length - 1;
              return (
                <div key={i} className={i === 0 ? undefined : 'l-prod'}>
                  {line}
                  {isLast && phase === 'streaming' && <span className="l-caret" />}
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}

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
          <ChatDemo />
        </div>
      </header>

      <section className="l-container l-section">
        <Reveal>
          <div className="l-section-head">
            <div className="l-eyebrow">WHY IT'S DIFFERENT</div>
            <h2 className="l-h2">A shopping assistant that actually reasons.</h2>
          </div>
        </Reveal>
        <Reveal>
          <div className="l-grid">
            {features.map((f) => (
              <div className="l-card" key={f.title}>
                <div className="l-card-icon"><f.icon size={18} /></div>
                <h3 className="l-card-title">{f.title}</h3>
                <p className="l-card-body">{f.body}</p>
              </div>
            ))}
          </div>
        </Reveal>
      </section>

      <section className="l-container l-section">
        <Reveal>
          <div className="l-section-head">
            <div className="l-eyebrow">HOW IT WORKS</div>
            <h2 className="l-h2">Three steps, one message.</h2>
          </div>
        </Reveal>
        <Reveal>
          <div className="l-steps">
            {steps.map((s) => (
              <div className="l-step" key={s.n}>
                <div className="l-step-n">{s.n}</div>
                <h3 className="l-card-title">{s.title}</h3>
                <p className="l-card-body">{s.body}</p>
              </div>
            ))}
          </div>
        </Reveal>
      </section>

      <section className="l-container l-cta-wrap">
        <Reveal>
          <div className="l-cta">
            <h2 className="l-h2">Ready to try it?</h2>
            <p className="l-lead l-cta-lead">Create an account and start asking in seconds.</p>
            <button className="l-btn l-btn-primary l-btn-lg" onClick={onGetStarted}>
              Get started <ArrowRight size={16} />
            </button>
          </div>
        </Reveal>
      </section>

      <footer className="l-footer">
        <div className="l-container l-footer-inner">
          <div className="l-brand l-brand-static">
            <ShoppingBag size={16} />
            <span>Ecommerce Agent</span>
          </div>
          <span className="l-foot-meta">
            © 2026 Ecommerce Agent. All rights reserved. · Built with Gemini · React + FastAPI
          </span>
        </div>
      </footer>
    </div>
  );
};

export default LandingPage;
