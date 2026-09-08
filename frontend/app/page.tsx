import CtaStrip from '@/components/cta-strip';
import Features from '@/components/features';
import Hero from '@/components/hero';
import PageFoot from '@/components/page-foot';
import TopNav from '@/components/top-nav';

export default function Page() {
  return (
    <>
      <TopNav />
      <main id="content">
        <Hero />
        <Features />
        <CtaStrip />
      </main>
      <PageFoot />
    </>
  );
}
