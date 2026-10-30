from datetime import datetime, timedelta
import unittest

from rpa.sync.transform import calcular_tempos_reais

INICIO = datetime(2026, 10, 29, 8, 0)


def parada(ponto, ordem, rota="r1"):
    return {"rota_id": rota, "ponto_id": ponto, "ordem": ordem}


def feita(execucao, ponto, minutos, rota="r1", inicio=INICIO):
    return {"execucao_id": execucao, "rota_id": rota, "ponto_id": ponto,
            "data_inicio": inicio, "data_conclusao": inicio + timedelta(minutes=minutos)}


class TemposReaisTest(unittest.TestCase):
    def setUp(self):
        self.paradas = [parada("A", 1), parada("B", 2), parada("C", 3)]

    def por_ponto(self, tempos):
        return {t["ponto_id"]: (t["tempo_real_min"], t["amostras"]) for t in tempos}

    def test_trechos_na_mesma_ordem_do_grafo(self):
        tempos = calcular_tempos_reais(self.paradas, [feita("e1", "A", 12), feita("e1", "B", 30), feita("e1", "C", 41)])
        self.assertEqual(self.por_ponto(tempos), {"A": (12.0, 1), "B": (18.0, 1), "C": (11.0, 1)})

    def test_media_entre_viagens(self):
        execucoes = [feita("e1", "A", 10), feita("e2", "A", 20)]
        self.assertEqual(self.por_ponto(calcular_tempos_reais(self.paradas, execucoes)), {"A": (15.0, 2)})

    def test_ordem_diferente_so_aproveita_trechos_iguais(self):
        # o motorista fez A, C, B: A->C e C->B não são arestas do grafo (que espera A->B->C)
        execucoes = [feita("e1", "A", 10), feita("e1", "C", 25), feita("e1", "B", 33)]
        self.assertEqual(self.por_ponto(calcular_tempos_reais(self.paradas, execucoes)), {"A": (10.0, 1)})

    def test_coleta_pulada_quebra_a_sequencia(self):
        # B não foi concluída: o trecho até C saiu de A, não de B
        execucoes = [feita("e1", "A", 10), feita("e1", "C", 40)]
        self.assertEqual(self.por_ponto(calcular_tempos_reais(self.paradas, execucoes)), {"A": (10.0, 1)})

    def test_descarta_trecho_absurdo_e_parada_de_outra_rota(self):
        execucoes = [feita("e1", "A", 600), feita("e2", "X", 5), feita("e3", "A", 0)]
        self.assertEqual(calcular_tempos_reais(self.paradas, execucoes), [])

    def test_mais_de_dez_paradas_ordenadas_por_numero(self):
        paradas = [parada(f"P{i}", i) for i in range(1, 13)]
        execucoes = [feita("e1", f"P{i}", i * 5) for i in range(1, 13)]
        tempos = self.por_ponto(calcular_tempos_reais(paradas, execucoes))
        self.assertEqual(len(tempos), 12)
        self.assertEqual(tempos["P11"], (5.0, 1))


if __name__ == "__main__":
    unittest.main()
