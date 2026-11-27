<p align="center">
  <a href="README.ja.md">日本語</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.md">English</a> | <a href="README.it.md">Italiano</a> | <a href="README.pt-BR.md">Português (BR)</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/mcp-tool-shop-org/brand/main/logos/colab-runner/readme.png" alt="Colab Runner" width="400">
</p>

<p align="center">
  <a href="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml"><img src="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://mcp-tool-shop-org.github.io/colab-runner/"><img src="https://img.shields.io/badge/Landing_Page-live-blue" alt="Landing Page"></a>
</p>

अपने कंप्यूटर से एक भुगतान किए गए Google Colab GPU पर एक स्क्रिप्ट चलाएं, एक निश्चित खर्च सीमा निर्धारित करें, और परिणाम प्राप्त करें। आप एक छोटी फ़ाइल में कार्य का वर्णन करते हैं; colab-runner सत्र खोलता है, कार्य चलाता है, आउटपुट को आपके कंप्यूटर पर लाता है, सत्र को बंद करता है, और यह बताता है कि इसमें कितना खर्च आया।

## यह क्या करता है

- **प्रत्येक कार्य के लिए एक विशिष्ट फ़ाइल।** इनपुट, सेटअप और रन कमांड, आउटपुट, GPU प्रकार, खर्च सीमा और समय सीमा, ये सभी `colab-window.toml` में शामिल हैं। प्रत्येक कार्य के लिए अलग से लॉन्च स्क्रिप्ट की आवश्यकता नहीं है।
- **आपके द्वारा निर्धारित खर्च सीमा।** विंडो की कीमत आपके कंप्यूटर पर बिताए गए समय के आधार पर निर्धारित की जाती है। जब यह सीमा या समय सीमा तक पहुँच जाता है, तो कार्य बंद हो जाता है और सत्र समाप्त हो जाता है।
- **भुगतान करने से पहले इसे आज़माएँ।** `--dry` बिना Colab कॉल के विशिष्ट फ़ाइल की जाँच करता है। `--local-cpu` आपके कंप्यूटर पर पूरे कार्य को चलाता है, ठीक उसी तरह जैसे यह VM पर चलता है।
- **सत्र हमेशा बंद रहता है।** किसी भी तरह से, रन को समाप्त करने का प्रयास सत्र को बंद करने का होता है। यदि आपका कंप्यूटर रन के बीच में बंद हो जाता है, तो `stop --out-dir` बाद में इसे बंद कर देता है।
- **परिणामों से टोकन हटा दिए जाते हैं।** लॉग और डाउनलोड की गई फ़ाइलों से एक्सेस टोकन हटा दिए जाते हैं, इससे पहले कि वे आपके कंप्यूटर पर आएं।
- **प्रत्येक रन का रिकॉर्ड।** `outcome.json` आउटपुट फ़ोल्डर में बताता है कि रन कैसे समाप्त हुआ और इसमें कितना खर्च आया।

## इंस्टॉल करें

colab-runner PyPI पर उपलब्ध नहीं है। इसे रिलीज़ टैग से इंस्टॉल करें:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.2.0"
```

आपको Linux या WSL, Python 3.11 या उसके बाद का संस्करण, और Google के `colab` कमांड लाइन की आवश्यकता होगी, और उस खाते में लॉग इन होना होगा जो भुगतान करता है। colab-runner की कोई अन्य निर्भरता नहीं है।

## त्वरित शुरुआत

अपने प्रोजेक्ट के गिट रिपॉजिटरी के अंदर काम करें: इसके बाहर की फाइलें सुरक्षित डेटा के रूप में गिनी जाती हैं और उन्हें अस्वीकार कर दिया जाता है। एक विशिष्ट विवरण लिखें (एक पूर्ण विवरण [examples/merchant_league_seats](examples/merchant_league_seats/README.md) में है):

```toml
schema_version = 1

[project]
repo = "you/your-repo"   # required: your repo, for the records
study = "first-try"      # required: a name for this line of work
job = "train_01"

[compute]
card = "L4"        # CPU, T4, L4, G4 or A100
shape = "standard"
max_rate = 2.5     # expected compute units per hour
cap = 5.0          # never spend more than this
wall_s = 3600      # never run longer than this

[inputs]
root = "."
files = [
  { src = "train.py", dst = "train.py" },
  { src = "requirements.txt", dst = "requirements.txt" },
]

[job]
setup = ["python3 -m pip install -r requirements.txt"]
run = ["python3 train.py"]
cpu_dry_run = ["python3 train.py --smoke"]

[outputs]
collect = ["model", "metrics.json"]
```

`--local-cpu` आपके मशीन पर सेटअप और जॉब कमांड चलाता है, इसलिए इसे उन सभी चीज़ों की आवश्यकता होती है जिन्हें वे कहते हैं (यहां पिप: `sudo apt install python3-pip`, एक नए उबंटू या डब्ल्यूएसएल पर)। फिर इसे जांचें, इसका पूर्वाभ्यास करें और इसे चलाएं:

```bash
python -m colab_runner launch --spec colab-window.toml --out-dir runs/try --dry
python -m colab_runner launch --spec colab-window.toml --out-dir runs/cpu --local-cpu
python -m colab_runner launch --spec colab-window.toml --out-dir runs/w1 --cap 5 --max-rate 2.5
python -m colab_runner status --out-dir runs/w1
```

आउटपुट `runs/w1/collected/` में आते हैं, और उनके बगल में `outcome.json` होता है। लाइव लॉन्च के लिए `--cap` और `--max-rate` कमांड लाइन पर होने चाहिए, इसलिए भुगतान किए गए रन हमेशा जानबूझकर किए जाते हैं।

## जब कोई रन समाप्त होता है

| बाहर निकलें | अर्थ |
| --- | --- |
| 0 | कार्य समाप्त हो गया, आउटपुट वापस आ गए, और सत्र बंद हो गया। |
| 1 | इससे पहले कि कुछ भी खोला जाए, यह विफल हो गया: विशिष्ट फ़ाइल या फ़्लैग को ठीक करें (संदेश में बताया गया है)। |
| 2 | रन जल्दी समाप्त हो गया (कार्य विफल, सीमा, समय सीमा, रुका या दुर्गम)। सत्र बंद हो गया; यदि बंद करने में विफलता हुई, तो संदेश में `stop` चलाने के लिए कहा गया है। |
| 3 | कार्य समाप्त हो गया, लेकिन बंद की पुष्टि नहीं की गई। `python -m colab_runner stop --out-dir D` चलाएँ। |

प्रत्येक अस्वीकृति और त्रुटि एक कोड, एक संदेश और एक संकेत प्रिंट करती है। `python -m colab_runner --help` कमांड की सूची देता है, और `launch --help` प्रत्येक फ़्लैग की सूची देता है।

**अधिक विवरण:** स्वयं द्वारा खोले गए सत्र से जुड़ना, कार्य को क्या लिखना चाहिए ताकि प्रगति दिखाई दे, डाउनलोड कैसे साफ़ किए जाते हैं, और मूल्य निर्धारण, ये सभी [docs/reference.md](docs/reference.md) में हैं।

## सुरक्षा और खतरे का मॉडल

**यह क्या प्रभावित करता है।**
- आपके द्वारा नामित कोलाब सत्र: यह सेल फाइलें अपलोड करता है, उस जॉब को शुरू और बंद करता है जिसे उसने शुरू किया था, उन फाइलों को डाउनलोड करता है जिनकी मांग की गई थी, और सत्र को बंद कर देता है। यह कभी भी किसी ऐसे प्रोसेस को संकेत नहीं भेजता है जो कोलाब-रनर (`CR_NOT_OURS`) के किसी अन्य रन से संबंधित है।
- आपकी मशीन पर आउट डाइर (`--out-dir`) और इसका संबंधित क्वारंटाइन `<out-dir>.quarantine/`। एक साधारण `Colab.download` जिसमें कोई क्वारंटाइन डाइर नहीं है, सिस्टम टेम्प डाइर के अंतर्गत एक नया `colab-runner-quarantine-*` फ़ोल्डर उपयोग करता है।
- पर्यावरण चर जिन्हें आप `--env` के साथ नामित करते हैं, जिसे यह सेल फाइल के अंदर वीएम को पास करता है।

**यह क्या नहीं छूता।** आउट निर्देशिका के बाहर कोई भी फ़ाइल, इसका संगरोध और अस्थायी संगरोध। कोई अन्य Colab सत्र नहीं। कोई खाता सेटिंग, क्रेडेंशियल या बिलिंग नहीं: यह खाते की दर और शेष राशि को `colab` कमांड लाइन के माध्यम से पढ़ता है और कभी भी उन्हें नहीं बदलता है।

**नेटवर्क।** केवल `colab` कमांड लाइन के माध्यम से, Google Colab तक। colab-runner स्वयं कोई कनेक्शन नहीं खोलता है।

**गुप्त जानकारी।** एक गुप्त जानकारी जिसे `--env NAME` के रूप में पास किया जाता है, वह किसी भी कमांड लाइन से अलग रहती है। प्रत्येक आउटपुट लाइन, त्रुटि लाइन, ट्रेसबैक और डाउनलोड की गई टेक्स्ट फाइल से टोकन हटा दिए जाते हैं; एक बाइनरी फाइल जिसमें एक टोकन होता है, उसे बिना बदले क्वारंटाइन किया जाता है और डाउनलोड को अस्वीकार कर दिया जाता है (ऊपर)। कोलाब-रनर कभी भी क्वारंटाइन को नहीं हटाता है: आपको इसे हटाना होगा, और इसे कभी भी साझा नहीं करना चाहिए।

**अनुमतियाँ।** यह उस उपयोगकर्ता के रूप में चलता है जो इसे चलाता है, उस `colab` साइन-इन के साथ जो उस उपयोगकर्ता के पास पहले से है। इसे किसी भी उन्नयन की आवश्यकता नहीं है।

**कोई टेलीमेट्री नहीं।** colab-runner कुछ भी एकत्र या नहीं भेजता है।

किसी भेद्यता की रिपोर्ट करना: [SECURITY.md](SECURITY.md) देखें।

## यहाँ क्या नहीं है

कोई बकेट नाम, खाता नाम, लॉन्च रैपर या स्थानीय पथ नहीं। एक कॉलर उन्हें फ़्लैग या पर्यावरण चर द्वारा पास करता है; परीक्षण प्लेसहोल्डर का उपयोग करते हैं।

## सत्यापित करें

```bash
bash verify.sh
```

## लाइसेंस

MIT. [LICENSE](LICENSE) देखें।

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
