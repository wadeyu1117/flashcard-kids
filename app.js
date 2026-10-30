(() => {
  "use strict";

  // ==================== STATE ====================
  let catalog = null;
  let allCardsFlat = [];
  let currentDeckIndex = 0;
  let currentFilter = "unit"; // 'unit' | 'starred' | 'random'
  let activeCards = [];
  let currentCardIndex = 0;
  let isFlipped = false;
  let frontIsPic = true; // true: Front=Picture, Back=Word; false: Front=Word, Back=Picture
  let autoSpeak = true;
  let currentMode = "single"; // 'single' | 'sheet' | 'quiz'
  let sheetSideBySide = false;

  // Persistent learning progress
  const STORAGE_STARRED = "kids_fc_starred_v1";
  const STORAGE_MASTERED = "kids_fc_mastered_v1";
  let starredSet = new Set(JSON.parse(localStorage.getItem(STORAGE_STARRED) || "[]"));
  let masteredSet = new Set(JSON.parse(localStorage.getItem(STORAGE_MASTERED) || "[]"));

  // Quiz state
  let quizScore = 0;
  let quizStreak = 0;
  let quizCurrentCard = null;
  let quizLocked = false;

  // ==================== DOM ELEMENTS ====================
  const elUnitBadge = document.getElementById("current-unit-badge");
  const elUnitTitle = document.getElementById("current-unit-title");
  const elCountUnit = document.getElementById("count-unit");
  const elCountStarred = document.getElementById("count-starred");
  const elCountTotal = document.getElementById("count-total");

  const elCardPosCurrent = document.getElementById("card-pos-current");
  const elCardPosTotal = document.getElementById("card-pos-total");
  const elProgressDots = document.getElementById("progress-dots");
  const elUnitMasteredCount = document.getElementById("unit-mastered-count");

  const elFlashcard3D = document.getElementById("flashcard-3d");
  const elFrontTagLabel = document.getElementById("front-tag-label");
  const elBackTagLabel = document.getElementById("back-tag-label");
  const elSingleFrontImg = document.getElementById("single-front-img");
  const elSingleBackImg = document.getElementById("single-back-img");
  const elSingleWordMain = document.getElementById("single-word-main");
  const elSingleWordSub = document.getElementById("single-word-sub");
  const elSingleWordZh = document.getElementById("single-word-zh");
  const elBtnPeekZh = document.getElementById("btn-peek-zh");
  const elFrontZhHint = document.getElementById("front-zh-hint");

  const elBtnStar = document.getElementById("btn-star-card");
  const elStarIcon = document.getElementById("star-icon");
  const elStarLabel = document.getElementById("star-label");
  const elBtnMaster = document.getElementById("btn-master-card");
  const elMasterIcon = document.getElementById("master-icon");
  const elMasterLabel = document.getElementById("master-label");

  const elSheetWrap = document.getElementById("sheet-interactive-wrap");
  const elSheetBaseImg = document.getElementById("sheet-base-img");
  const elSheetHotspots = document.getElementById("sheet-hotspots");
  const elSheetSecondBoard = document.getElementById("sheet-second-board");
  const elSheetSecondImg = document.getElementById("sheet-second-img");

  const elAudioDrawer = document.getElementById("audio-drawer");
  const elAudioSelect = document.getElementById("audio-select");
  const elAudioPlayer = document.getElementById("main-audio-player");
  const elTtsPlayer = document.getElementById("tts-audio-player") || new Audio();

  const elUnitModal = document.getElementById("unit-modal");
  const elUnitGrid = document.getElementById("unit-grid");
  const elUnitSearch = document.getElementById("unit-search-input");
  const elModalDeckCount = document.getElementById("modal-deck-count");
  const elToast = document.getElementById("toast");

  // ==================== SOUND EFFECTS & TTS ====================
  let audioCtx = null;
  let currentTtsSource = null;
  let userHasInteracted = false;
  const ttsBufferCache = new Map();

  function ensureAudioUnlocked() {
    userHasInteracted = true;
    if ("audioSession" in navigator) {
      try {
        navigator.audioSession.type = "playback";
      } catch (_) {}
    }
    try {
      if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (audioCtx.state === "suspended") {
        audioCtx.resume();
      }
    } catch (_) {}
  }

  ["click", "touchstart", "keydown"].forEach((evt) => {
    window.addEventListener(evt, ensureAudioUnlocked, { once: true, passive: true });
  });

  function playEffect(type) {
    try {
      ensureAudioUnlocked();
      if (!audioCtx) return;
      const now = audioCtx.currentTime;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.connect(gain);
      gain.connect(audioCtx.destination);

      if (type === "success") {
        osc.type = "triangle";
        osc.frequency.setValueAtTime(523.25, now); // C5
        osc.frequency.setValueAtTime(659.25, now + 0.09); // E5
        osc.frequency.setValueAtTime(783.99, now + 0.18); // G5
        gain.gain.setValueAtTime(0.18, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.42);
        osc.start(now);
        osc.stop(now + 0.42);
      } else if (type === "flip") {
        osc.type = "sine";
        osc.frequency.setValueAtTime(320, now);
        osc.frequency.exponentialRampToValueAtTime(520, now + 0.06);
        gain.gain.setValueAtTime(0.06, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.07);
        osc.start(now);
        osc.stop(now + 0.07);
      } else if (type === "wrong") {
        osc.type = "sawtooth";
        osc.frequency.setValueAtTime(220, now);
        osc.frequency.setValueAtTime(175, now + 0.12);
        gain.gain.setValueAtTime(0.12, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.30);
        osc.start(now);
        osc.stop(now + 0.30);
      }
    } catch (_) {}
  }

  function setSpeakingIndicator(active, isSlow = false) {
    const btnFront = document.getElementById("btn-card-speak-front");
    const btnBack = document.getElementById("btn-card-speak-back");
    const btnNorm = document.getElementById("btn-speak-normal");
    const btnSlow = document.getElementById("btn-speak-slow");
    if (btnFront) btnFront.classList.toggle("speaking", active);
    if (btnBack) btnBack.classList.toggle("speaking", active);
    if (btnNorm) btnNorm.classList.toggle("speaking", active && !isSlow);
    if (btnSlow) btnSlow.classList.toggle("speaking", active && isSlow);
  }

  function fallbackWebSpeech(text, rate = 0.90) {
    if (!("speechSynthesis" in window) || !text) return;
    try {
      window.speechSynthesis.cancel();
      const clean = text.replace(/["[\]/]/g, " ").trim();
      setTimeout(() => {
        const utter = new SpeechSynthesisUtterance(clean);
        utter.lang = "en-US";
        utter.rate = rate;
        utter.pitch = 1.02;
        const voices = window.speechSynthesis.getVoices();
        const preferred =
          voices.find((v) => v.lang === "en-US" && /Google|Samantha|Natural|Microsoft Aria/i.test(v.name)) ||
          voices.find((v) => v.lang.startsWith("en-US")) ||
          voices.find((v) => v.lang.startsWith("en"));
        if (preferred) utter.voice = preferred;
        utter.onend = () => setSpeakingIndicator(false);
        utter.onerror = () => setSpeakingIndicator(false);
        window.speechSynthesis.speak(utter);
      }, 30);
    } catch (_) {
      setSpeakingIndicator(false);
    }
  }

  async function playMp3ViaWebAudio(url, textFallback, rate) {
    try {
      ensureAudioUnlocked();
      if (!audioCtx) throw new Error("No AudioContext");
      if (currentTtsSource) {
        try {
          currentTtsSource.stop();
        } catch (_) {}
        currentTtsSource = null;
      }
      let audioBuf = ttsBufferCache.get(url);
      if (!audioBuf) {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const arr = await resp.arrayBuffer();
        audioBuf = await audioCtx.decodeAudioData(arr);
        ttsBufferCache.set(url, audioBuf);
      }
      const src = audioCtx.createBufferSource();
      src.buffer = audioBuf;
      src.connect(audioCtx.destination);
      src.onended = () => setSpeakingIndicator(false);
      currentTtsSource = src;
      src.start(0);
    } catch (_) {
      fallbackWebSpeech(textFallback, rate);
    }
  }

  function speakText(cardOrText, rate = 0.90) {
    ensureAudioUnlocked();
    const isSlow = rate < 0.75;
    let cardObj = null;
    let wordStr = "";

    if (cardOrText && typeof cardOrText === "object") {
      cardObj = cardOrText;
      wordStr = cardObj.word || "";
    } else if (typeof cardOrText === "string") {
      wordStr = cardOrText.trim();
      cardObj = allCardsFlat.find((c) => c.word.toLowerCase() === wordStr.toLowerCase()) || null;
    }

    if (!wordStr) return;
    const mp3Url = cardObj
      ? resolveUrl(isSlow ? cardObj.tts_slow_url || cardObj.tts_url : cardObj.tts_url)
      : "";

    setSpeakingIndicator(true, isSlow);

    if (mp3Url) {
      try {
        if (currentTtsSource) {
          try {
            currentTtsSource.stop();
          } catch (_) {}
          currentTtsSource = null;
        }
        elTtsPlayer.pause();
        elTtsPlayer.src = mp3Url;
        elTtsPlayer.playbackRate = 1.0;
        elTtsPlayer.onended = () => setSpeakingIndicator(false);
        elTtsPlayer.onerror = () => playMp3ViaWebAudio(mp3Url, wordStr, rate);
        const playPromise = elTtsPlayer.play();
        if (playPromise !== undefined) {
          playPromise.catch(() => {
            playMp3ViaWebAudio(mp3Url, wordStr, rate);
          });
        }
        return;
      } catch (_) {
        playMp3ViaWebAudio(mp3Url, wordStr, rate);
        return;
      }
    }

    fallbackWebSpeech(wordStr, rate);
  }

  function preloadCardAudio(card) {
    if (!card) return;
    [card.tts_url, card.tts_slow_url].forEach((u) => {
      const url = resolveUrl(u);
      if (url && !ttsBufferCache.has(url)) {
        fetch(url).catch(() => {});
      }
    });
  }

  if ("speechSynthesis" in window) {
    window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
  }

  function showToast(msg, duration = 2800) {
    elToast.textContent = msg;
    elToast.classList.remove("hidden");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => {
      elToast.classList.add("hidden");
    }, duration);
  }

  function resolveUrl(u) {
    if (!u) return "";
    return u.replace(/^\/+/, "").replace(/#/g, "%23");
  }

  function saveProgress() {
    localStorage.setItem(STORAGE_STARRED, JSON.stringify([...starredSet]));
    localStorage.setItem(STORAGE_MASTERED, JSON.stringify([...masteredSet]));
    updateCountsUI();
  }

  // ==================== CATALOG & DECK MANAGEMENT ====================
  async function loadCatalog() {
    try {
      let resp = await fetch("data/catalog.json?t=" + Date.now());
      if (!resp.ok) {
        resp = await fetch("/api/catalog");
      }
      catalog = await resp.json();
      rebuildFlatCards();
      initAudioTracks();
      selectDeck(0);
      renderUnitModalGrid();
    } catch (err) {
      showToast("⚠️ 無法載入字卡資料，請稍後再試");
      console.error(err);
    }
  }

  function rebuildFlatCards() {
    allCardsFlat = [];
    if (!catalog || !catalog.decks) return;
    catalog.decks.forEach((deck, dIdx) => {
      deck.pic_sheet_url = resolveUrl(deck.pic_sheet_url);
      deck.word_sheet_url = resolveUrl(deck.word_sheet_url);
      deck.cards.forEach((c) => {
        c.pic_crop_url = resolveUrl(c.pic_crop_url);
        c.word_crop_url = resolveUrl(c.word_crop_url);
        c.tts_url = resolveUrl(c.tts_url);
        c.tts_slow_url = resolveUrl(c.tts_slow_url);
        allCardsFlat.push({
          ...c,
          deckIndex: dIdx,
          deckTitleZh: deck.title_zh,
          deckTitleEn: deck.title_en,
          deckDate: deck.date,
        });
      });
    });
    updateCountsUI();
  }

  function updateCountsUI() {
    if (!catalog || !catalog.decks.length) return;
    const deck = catalog.decks[currentDeckIndex];
    elCountUnit.textContent = deck ? deck.cards.length : 0;
    elCountStarred.textContent = allCardsFlat.filter((c) => starredSet.has(c.id)).length;
    elCountTotal.textContent = allCardsFlat.length;
  }

  function initAudioTracks() {
    if (!catalog || !catalog.audio_tracks) return;
    elAudioSelect.innerHTML = "";
    catalog.audio_tracks.forEach((tr) => {
      tr.url = resolveUrl(tr.url);
      const opt = document.createElement("option");
      opt.value = tr.url;
      opt.textContent = tr.title;
      elAudioSelect.appendChild(opt);
    });
    if (catalog.audio_tracks.length > 0) {
      elAudioPlayer.src = catalog.audio_tracks[0].url;
    }
  }

  function selectDeck(index) {
    if (!catalog || !catalog.decks.length) return;
    currentDeckIndex = Math.max(0, Math.min(catalog.decks.length - 1, index));
    currentFilter = "unit";
    updateFilterPillsUI();
    rebuildActiveCards();
  }

  function setFilterMode(filter) {
    if (filter === "starred") {
      const starredCards = allCardsFlat.filter((c) => starredSet.has(c.id));
      if (starredCards.length === 0) {
        showToast("💡 目前還沒有標記「⭐ 加入複習」的字卡喔！可在字卡下方點擊「☆ 加入複習」。");
        return;
      }
    }
    currentFilter = filter;
    updateFilterPillsUI();
    rebuildActiveCards();
  }

  function updateFilterPillsUI() {
    document.querySelectorAll(".filter-pill").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.filter === currentFilter);
    });
  }

  function shuffleArray(arr) {
    const copy = [...arr];
    for (let i = copy.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
  }

  function rebuildActiveCards() {
    if (!catalog || !catalog.decks.length) return;
    const deck = catalog.decks[currentDeckIndex];

    if (currentFilter === "unit") {
      activeCards = deck.cards.map((c) => ({
        ...c,
        deckIndex: currentDeckIndex,
        deckTitleZh: deck.title_zh,
        deckTitleEn: deck.title_en,
      }));
      elUnitBadge.textContent = `單元 #${deck.unit_number} · ${deck.date || "精選教材"}`;
      elUnitTitle.textContent = `${deck.title_zh} (${deck.title_en})`;
    } else if (currentFilter === "starred") {
      activeCards = allCardsFlat.filter((c) => starredSet.has(c.id));
      elUnitBadge.textContent = `⭐ 重點複習清單`;
      elUnitTitle.textContent = `共 ${activeCards.length} 張待複習字卡`;
    } else if (currentFilter === "random") {
      activeCards = shuffleArray(allCardsFlat);
      elUnitBadge.textContent = `🔀 全部字卡隨機抽考`;
      elUnitTitle.textContent = `全部 44 個單元 (${activeCards.length} 張)`;
    }

    currentCardIndex = 0;
    isFlipped = false;
    updateCountsUI();
    renderCurrentCard();
    renderInteractiveSheet();
    if (currentMode === "quiz") {
      startNextQuizQuestion();
    }
  }

  // ==================== MODE 1: SINGLE CARD RENDER ====================
  function renderCurrentCard(shouldSpeak = false) {
    if (!activeCards.length) return;
    const card = activeCards[currentCardIndex];
    preloadCardAudio(card);
    if (activeCards.length > 1) {
      preloadCardAudio(activeCards[(currentCardIndex + 1) % activeCards.length]);
    }

    // Reset flip state without jump
    isFlipped = false;
    elFlashcard3D.classList.remove("is-flipped");
    elBtnPeekZh.classList.remove("revealed");

    // Counters
    elCardPosCurrent.textContent = currentCardIndex + 1;
    elCardPosTotal.textContent = activeCards.length;

    const masteredInActive = activeCards.filter((c) => masteredSet.has(c.id)).length;
    elUnitMasteredCount.textContent = `${masteredInActive} / ${activeCards.length}`;

    // Progress dots (show up to 24 dots cleanly)
    elProgressDots.innerHTML = "";
    if (activeCards.length <= 24) {
      activeCards.forEach((c, idx) => {
        const dot = document.createElement("span");
        dot.className = "p-dot";
        if (idx === currentCardIndex) dot.classList.add("active");
        else if (masteredSet.has(c.id)) dot.classList.add("mastered");
        else if (starredSet.has(c.id)) dot.classList.add("starred");
        dot.title = `${idx + 1}. ${c.word}`;
        dot.addEventListener("click", () => {
          currentCardIndex = idx;
          renderCurrentCard(true);
        });
        elProgressDots.appendChild(dot);
      });
    }

    // Set front & back content based on frontIsPic
    if (frontIsPic) {
      elFrontTagLabel.textContent = "🖼️ 看圖猜單字";
      elBackTagLabel.textContent = "🔤 英語單字解答";
      elSingleFrontImg.src = card.pic_crop_url;
      elSingleBackImg.src = card.word_crop_url;
      elFrontZhHint.textContent = card.zh || card.word;
      elBtnPeekZh.classList.remove("hidden");
    } else {
      elFrontTagLabel.textContent = "🔤 看單字想圖片";
      elBackTagLabel.textContent = "🖼️ 圖片與中文解答";
      elSingleFrontImg.src = card.word_crop_url;
      elSingleBackImg.src = card.pic_crop_url;
      elFrontZhHint.textContent = card.zh || "";
      elBtnPeekZh.classList.remove("hidden");
    }

    if (autoSpeak && (shouldSpeak || !frontIsPic) && userHasInteracted) {
      speakText(card, 0.90);
    }

    elSingleWordMain.textContent = card.word;
    elSingleWordSub.textContent = card.subtext || "";
    elSingleWordSub.classList.toggle("hidden", !card.subtext);
    elSingleWordZh.textContent = card.zh || "";

    // Star & Master button status
    const isStar = starredSet.has(card.id);
    elBtnStar.classList.toggle("active", isStar);
    elStarIcon.textContent = isStar ? "⭐" : "☆";
    elStarLabel.textContent = isStar ? "已加複習" : "加入複習";

    const isMaster = masteredSet.has(card.id);
    elBtnMaster.classList.toggle("active", isMaster);
    elMasterIcon.textContent = isMaster ? "✅" : "⬜";
    elMasterLabel.textContent = isMaster ? "已學會！" : "學會了";
  }

  function flipCurrentCard() {
    if (!activeCards.length) return;
    isFlipped = !isFlipped;
    elFlashcard3D.classList.toggle("is-flipped", isFlipped);
    playEffect("flip");

    const card = activeCards[currentCardIndex];
    if (autoSpeak) {
      speakText(card, 0.90);
    }
  }

  function goPrevCard() {
    if (!activeCards.length) return;
    currentCardIndex = (currentCardIndex - 1 + activeCards.length) % activeCards.length;
    renderCurrentCard(true);
  }

  function goNextCard() {
    if (!activeCards.length) return;
    currentCardIndex = (currentCardIndex + 1) % activeCards.length;
    renderCurrentCard(true);
  }

  // ==================== MODE 2: INTERACTIVE SHEET VIEW ====================
  function renderInteractiveSheet() {
    if (!catalog || !catalog.decks.length) return;
    const deck = catalog.decks[currentDeckIndex];
    if (!deck) return;

    const baseSheetUrl = frontIsPic ? deck.pic_sheet_url : deck.word_sheet_url;
    const secondSheetUrl = frontIsPic ? deck.word_sheet_url : deck.pic_sheet_url;

    elSheetBaseImg.src = baseSheetUrl;
    elSheetSecondImg.src = secondSheetUrl;
    elSheetHotspots.innerHTML = "";

    deck.cards.forEach((card) => {
      const box = frontIsPic ? card.pic_box_norm : card.word_box_norm;
      if (!box || box.length !== 4) return;
      const [ymin, xmin, ymax, xmax] = box;

      const spot = document.createElement("div");
      spot.className = "sheet-hotspot";
      spot.style.top = `${ymin / 10}%`;
      spot.style.left = `${xmin / 10}%`;
      spot.style.height = `${(ymax - ymin) / 10}%`;
      spot.style.width = `${(xmax - xmin) / 10}%`;
      spot.title = `點擊翻開：${card.word} (${card.zh})`;

      const backOverlay = document.createElement("div");
      backOverlay.className = "hotspot-card-back";

      const backImg = document.createElement("img");
      backImg.src = frontIsPic ? card.word_crop_url : card.pic_crop_url;
      backImg.alt = card.word;

      const cap = document.createElement("div");
      cap.className = "hotspot-caption";
      cap.textContent = `${card.word} · ${card.zh}`;

      backOverlay.appendChild(backImg);
      backOverlay.appendChild(cap);
      spot.appendChild(backOverlay);

      spot.addEventListener("click", (e) => {
        e.stopPropagation();
        const nowFlipped = !spot.classList.contains("flipped");
        spot.classList.toggle("flipped", nowFlipped);
        playEffect("flip");
        if (nowFlipped) {
          speakText(card, 0.90);
        }
      });

      elSheetHotspots.appendChild(spot);
    });
  }

  // ==================== MODE 3: 4-CHOICE QUIZ ====================
  function startNextQuizQuestion() {
    if (!allCardsFlat.length) return;
    quizLocked = false;
    const pool = activeCards.length >= 4 ? activeCards : allCardsFlat;
    const target = pool[Math.floor(Math.random() * pool.length)];
    quizCurrentCard = target;

    const elQuizImg = document.getElementById("quiz-img");
    const elQuizZh = document.getElementById("quiz-zh-prompt");
    const elFeedback = document.getElementById("quiz-Feedback");
    const elOptions = document.getElementById("quiz-options");

    elQuizImg.src = target.pic_crop_url;
    elQuizZh.textContent = `💡 中文提示：${target.zh || "請看圖選出正確英文"}`;
    elFeedback.classList.add("hidden");

    // Pick 3 distractors with distinct words
    const distractors = shuffleArray(
      allCardsFlat.filter((c) => c.word.toLowerCase() !== target.word.toLowerCase())
    ).slice(0, 3);

    const choices = shuffleArray([target, ...distractors]);
    elOptions.innerHTML = "";

    choices.forEach((choice) => {
      const btn = document.createElement("button");
      btn.className = "quiz-option-btn";
      btn.innerHTML = `
        <div class="quiz-opt-en">${choice.word}</div>
        <div class="quiz-opt-zh">${choice.subtext || ""}</div>
      `;
      btn.addEventListener("click", () => handleQuizAnswer(btn, choice));
      elOptions.appendChild(btn);
    });

    if (autoSpeak) {
      speakText(target, 0.90);
    }
  }

  function handleQuizAnswer(btn, choice) {
    if (quizLocked) return;
    const elFeedback = document.getElementById("quiz-Feedback");
    const isCorrect = choice.id === quizCurrentCard.id;

    if (isCorrect) {
      quizLocked = true;
      btn.classList.add("correct");
      quizScore += 1;
      quizStreak += 1;
      document.getElementById("quiz-score").textContent = quizScore;
      document.getElementById("quiz-streak").textContent = quizStreak;

      elFeedback.textContent = `🎉 太棒了！答對了：${quizCurrentCard.word} (${quizCurrentCard.zh})`;
      elFeedback.className = "quiz-feedback correct";
      playEffect("success");
      speakText(quizCurrentCard, 0.92);

      setTimeout(() => {
        startNextQuizQuestion();
      }, 1250);
    } else {
      btn.classList.add("wrong");
      quizStreak = 0;
      document.getElementById("quiz-streak").textContent = quizStreak;
      elFeedback.textContent = `💪 再試一次喔！聽聽看發音～`;
      elFeedback.className = "quiz-feedback wrong";
      playEffect("wrong");
      speakText(quizCurrentCard, 0.85);
    }
  }

  // ==================== UNIT MODAL & SEARCH ====================
  function renderUnitModalGrid(query = "") {
    if (!catalog || !catalog.decks) return;
    elModalDeckCount.textContent = catalog.decks.length;
    elUnitGrid.innerHTML = "";
    const q = query.trim().toLowerCase();

    catalog.decks.forEach((deck, idx) => {
      const wordsStr = deck.cards.map((c) => `${c.word} ${c.zh}`).join(" · ");
      if (q) {
        const matchTitle =
          deck.title_zh.toLowerCase().includes(q) ||
          deck.title_en.toLowerCase().includes(q) ||
          (deck.date && deck.date.includes(q)) ||
          wordsStr.toLowerCase().includes(q);
        if (!matchTitle) return;
      }

      const masteredCnt = deck.cards.filter((c) => masteredSet.has(c.id)).length;
      const cardEl = document.createElement("button");
      cardEl.className = `unit-card ${idx === currentDeckIndex && currentFilter === "unit" ? "active" : ""}`;
      cardEl.innerHTML = `
        <div class="unit-card-top">
          <span>單元 #${deck.unit_number} (${deck.date || "課堂字卡"})</span>
          <span>✅ ${masteredCnt}/${deck.card_count} 張</span>
        </div>
        <div class="unit-card-title">${deck.title_zh} · ${deck.title_en}</div>
        <div class="unit-card-words">${deck.cards.map((c) => c.word).join(", ")}</div>
      `;
      cardEl.addEventListener("click", () => {
        selectDeck(idx);
        elUnitModal.classList.add("hidden");
      });
      elUnitGrid.appendChild(cardEl);
    });
  }

  // ==================== GOOGLE DRIVE SYNC ====================
  async function syncFromDrive() {
    const syncBtn = document.getElementById("btn-sync-drive");
    const syncIcon = document.getElementById("sync-icon");
    syncIcon.classList.add("spinning");
    syncBtn.disabled = true;
    showToast("🔄 正在從 Google Drive 檢查並同步最新字卡...", 10000);

    try {
      let data = null;
      if (location.hostname.endsWith("github.io")) {
        const r = await fetch("data/catalog.json?t=" + Date.now());
        const cat = await r.json();
        data = { status: "ok", catalog: cat };
      } else {
        const resp = await fetch("/api/sync", { method: "POST" });
        data = await resp.json();
      }
      if (data.status === "ok" && data.catalog) {
        catalog = data.catalog;
        rebuildFlatCards();
        initAudioTracks();
        selectDeck(currentDeckIndex);
        renderUnitModalGrid(elUnitSearch.value);
        const newCnt = catalog.new_files_downloaded || 0;
        if (newCnt > 0) {
          showToast(`🎉 同步成功！新增了 ${newCnt} 個檔案，目前共 ${catalog.total_decks} 個單元。`);
        } else {
          showToast(`✅ 已是最新狀態！共 ${catalog.total_decks} 個單元、${catalog.total_cards} 張字卡。`);
        }
      } else {
        showToast("⚠️ 同步發生問題：" + (data.message || "未知錯誤"));
      }
    } catch (e) {
      showToast("⚠️ 無法連線至同步伺服器");
    } finally {
      syncIcon.classList.remove("spinning");
      syncBtn.disabled = false;
    }
  }

  // ==================== EVENT LISTENERS ====================
  // Flip card
  elFlashcard3D.addEventListener("click", (e) => {
    if (e.target.closest("#btn-peek-zh") || e.target.closest(".face-speak-btn")) return;
    flipCurrentCard();
  });

  ["btn-card-speak-front", "btn-card-speak-back"].forEach((id) => {
    const btn = document.getElementById(id);
    if (btn) {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!activeCards.length) return;
        speakText(activeCards[currentCardIndex], 0.90);
      });
    }
  });

  elBtnPeekZh.addEventListener("click", (e) => {
    e.stopPropagation();
    elBtnPeekZh.classList.toggle("revealed");
  });

  document.getElementById("btn-flip-card").addEventListener("click", flipCurrentCard);
  document.getElementById("btn-prev-side").addEventListener("click", goPrevCard);
  document.getElementById("btn-next-side").addEventListener("click", goNextCard);
  document.getElementById("btn-prev-bottom").addEventListener("click", goPrevCard);
  document.getElementById("btn-next-bottom").addEventListener("click", goNextCard);

  // Speak buttons
  document.getElementById("btn-speak-normal").addEventListener("click", () => {
    if (!activeCards.length) return;
    speakText(activeCards[currentCardIndex], 0.90);
  });

  document.getElementById("btn-speak-slow").addEventListener("click", () => {
    if (!activeCards.length) return;
    speakText(activeCards[currentCardIndex], 0.55);
  });

  // Star & Master buttons
  elBtnStar.addEventListener("click", () => {
    if (!activeCards.length) return;
    const c = activeCards[currentCardIndex];
    if (starredSet.has(c.id)) {
      starredSet.delete(c.id);
      showToast(`已從重點複習移除：${c.word}`);
    } else {
      starredSet.add(c.id);
      playEffect("flip");
      showToast(`⭐ 已加入重點複習：${c.word}`);
    }
    saveProgress();
    renderCurrentCard();
  });

  elBtnMaster.addEventListener("click", () => {
    if (!activeCards.length) return;
    const c = activeCards[currentCardIndex];
    if (masteredSet.has(c.id)) {
      masteredSet.delete(c.id);
    } else {
      masteredSet.add(c.id);
      playEffect("success");
      showToast(`🎉 太棒了！已學會「${c.word}」`);
    }
    saveProgress();
    renderCurrentCard();
  });

  // Mode Tabs
  document.querySelectorAll(".mode-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      currentMode = tab.dataset.mode;
      document.querySelectorAll(".mode-tab").forEach((t) => t.classList.toggle("active", t === tab));
      document.getElementById("view-single").classList.toggle("active", currentMode === "single");
      document.getElementById("view-sheet").classList.toggle("active", currentMode === "sheet");
      document.getElementById("view-quiz").classList.toggle("active", currentMode === "quiz");
      if (currentMode === "quiz") {
        startNextQuizQuestion();
      }
    });
  });

  // Filter Pills
  document.querySelectorAll(".filter-pill").forEach((pill) => {
    pill.addEventListener("click", () => setFilterMode(pill.dataset.filter));
  });

  // Front side toggle (Pic first vs Word first)
  const elBtnFrontSide = document.getElementById("btn-front-side");
  elBtnFrontSide.addEventListener("click", () => {
    frontIsPic = !frontIsPic;
    elBtnFrontSide.textContent = frontIsPic ? "🖼️ 先看圖片" : "🔤 先看單字";
    elBtnFrontSide.classList.toggle("active", !frontIsPic);
    renderCurrentCard();
    renderInteractiveSheet();
  });

  // Auto-speak toggle
  const elBtnAutoSpeak = document.getElementById("btn-auto-speak");
  elBtnAutoSpeak.addEventListener("click", () => {
    autoSpeak = !autoSpeak;
    elBtnAutoSpeak.textContent = autoSpeak ? "🔊 自動發音：開" : "🔇 自動發音：關";
    elBtnAutoSpeak.classList.toggle("active", autoSpeak);
  });

  // Sheet Mode buttons
  document.getElementById("btn-sheet-flip-all").addEventListener("click", () => {
    document.querySelectorAll(".sheet-hotspot").forEach((s) => s.classList.add("flipped"));
    playEffect("flip");
  });

  document.getElementById("btn-sheet-reset").addEventListener("click", () => {
    document.querySelectorAll(".sheet-hotspot").forEach((s) => s.classList.remove("flipped"));
    playEffect("flip");
  });

  document.getElementById("btn-sheet-sidebyside").addEventListener("click", (e) => {
    sheetSideBySide = !sheetSideBySide;
    elSheetWrap.classList.toggle("side-by-side", sheetSideBySide);
    elSheetSecondBoard.classList.toggle("hidden", !sheetSideBySide);
    e.currentTarget.classList.toggle("primary", sheetSideBySide);
  });

  // Quiz speak button
  document.getElementById("btn-quiz-speak").addEventListener("click", () => {
    if (quizCurrentCard) speakText(quizCurrentCard, 0.88);
  });

  // Audio Drawer
  document.getElementById("btn-toggle-audio").addEventListener("click", () => {
    elAudioDrawer.classList.toggle("hidden");
  });
  document.getElementById("btn-close-audio").addEventListener("click", () => {
    elAudioDrawer.classList.add("hidden");
    elAudioPlayer.pause();
  });
  elAudioSelect.addEventListener("change", () => {
    elAudioPlayer.src = elAudioSelect.value;
    elAudioPlayer.play().catch(() => {});
  });

  // Unit Picker Modal
  document.getElementById("btn-open-drawer").addEventListener("click", () => {
    renderUnitModalGrid(elUnitSearch.value);
    elUnitModal.classList.remove("hidden");
  });
  document.getElementById("btn-close-modal").addEventListener("click", () => {
    elUnitModal.classList.add("hidden");
  });
  elUnitModal.addEventListener("click", (e) => {
    if (e.target === elUnitModal) elUnitModal.classList.add("hidden");
  });
  elUnitSearch.addEventListener("input", () => {
    renderUnitModalGrid(elUnitSearch.value);
  });

  // Sync Button
  document.getElementById("btn-sync-drive").addEventListener("click", syncFromDrive);

  // Keyboard shortcuts
  window.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (currentMode !== "single") return;
    if (e.code === "Space" || e.code === "ArrowUp" || e.code === "ArrowDown") {
      e.preventDefault();
      flipCurrentCard();
    } else if (e.code === "ArrowLeft") {
      e.preventDefault();
      goPrevCard();
    } else if (e.code === "ArrowRight") {
      e.preventDefault();
      goNextCard();
    }
  });

  // Touch swipe support on Flashcard
  let touchStartX = 0;
  let touchStartY = 0;
  elFlashcard3D.addEventListener(
    "touchstart",
    (e) => {
      if (e.touches.length === 1) {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
      }
    },
    { passive: true }
  );

  elFlashcard3D.addEventListener(
    "touchend",
    (e) => {
      if (e.changedTouches.length === 1) {
        const dx = e.changedTouches[0].clientX - touchStartX;
        const dy = e.changedTouches[0].clientY - touchStartY;
        if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
          if (dx < 0) goNextCard();
          else goPrevCard();
        }
      }
    },
    { passive: true }
  );

  // Register PWA Service Worker
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("sw.js").catch(() => {});
    });
  }

  // Start app
  loadCatalog();
})();
